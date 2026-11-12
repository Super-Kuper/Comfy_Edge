"""RunPod handler контракта `h3-serverless/v1.1`.

Поток: валидация job/input (включая ``input.assets``) → ``action:"health"`` или
``wait_ready`` → доставка assets → ``submit`` → ``wait`` → ``fetch_output`` →
временный файл → ``ffprobe`` → телеметрия из фактов исполнения → доставка.
Отмена (``asyncio.CancelledError``) → ``POST /interrupt`` в ComfyUI → исключение
наружу (job FAILED). Внешние зависимости инъектируются
(``build_handler``/``Dependencies``): тесты идут без сети, GPU и runpod.

Ссылки: `contract/worker-contract.md` §1/§4.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .assets import Asset, download_assets, validate_assets
from .comfy_client import ComfyClient
from .config import Config
from .errors import WorkerError
from .health import health_payload
from .media import MediaInfo, probe_media
from .storage import OutputStorage, deliver_output
from .telemetry import build_telemetry

_LOG = logging.getLogger(__name__)

# Инъекции для тестов: реальные функции — дефолты Dependencies/build_handler.
AssetsDownloader = Callable[[list[Asset], Config], Awaitable[list[Path]]]
MediaProber = Callable[..., Awaitable[MediaInfo]]
LoraHasher = Callable[[str], str | None]

# LoRA читается кусками: файл на томе может быть сотни МБ, в память целиком не берём.
_LORA_CHUNK_SIZE = 1024 * 1024


@dataclass
class Dependencies:
    """Всё внешнее, что нужно handler'у (инъекция для тестов)."""

    config: Config
    comfy: ComfyClient
    storage: OutputStorage | None = None
    assets_downloader: AssetsDownloader = download_assets
    media_prober: MediaProber = probe_media
    # None → реальный sha256 файла `${MODELS_ROOT}/loras/<lora_name>`.
    lora_hasher: LoraHasher | None = None


def _job_id(job: Any) -> str:
    if isinstance(job, Mapping) and job.get("id") is not None:
        return str(job.get("id"))
    return "<без id>"


def _validate_job(job: Any) -> Mapping[str, Any]:
    """``job.input`` обязателен и должен быть объектом (`contract/input.schema.json`)."""

    if not isinstance(job, Mapping):
        raise WorkerError("input_invalid", "job должен быть объектом RunPod")
    payload = job.get("input")
    if not isinstance(payload, Mapping):
        raise WorkerError("input_invalid", "job.input отсутствует или не является объектом")
    return payload


def _optional_mapping(payload: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = payload.get(name)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise WorkerError("input_invalid", f"input.{name}: ожидался объект")
    return value


def make_lora_hasher(models_root: str) -> LoraHasher:
    """sha256 файла LoRA с тома: ``${MODELS_ROOT}/loras/<lora_name>``.

    Файла нет/имя выходит за каталог → ``lora_hash_unavailable`` (видимая ошибка,
    `contract/worker-contract.md` §2: при ``use_turbo`` hash обязателен).
    """

    root = Path(models_root) / "loras"

    def hash_lora(name: str) -> str:
        if not isinstance(name, str) or not name.strip():
            raise WorkerError("lora_hash_unavailable", f"lora_name={name!r} — ожидалось имя файла")
        try:
            resolved_root = root.resolve()
            resolved = (root / name).resolve()
        except OSError as exc:
            raise WorkerError(
                "lora_hash_unavailable",
                f"не удалось разобрать путь LoRA {name!r} в {root}: {exc}",
            ) from exc
        if not resolved.is_relative_to(resolved_root):
            raise WorkerError(
                "lora_hash_unavailable",
                f"lora_name={name!r} выходит за MODELS_ROOT/loras ({root})",
            )
        if not resolved.is_file():
            raise WorkerError(
                "lora_hash_unavailable",
                f"файл LoRA не найден на томе: {resolved} (MODELS_ROOT={models_root})",
            )
        digest = hashlib.sha256()
        try:
            with resolved.open("rb") as handle:
                for chunk in iter(lambda: handle.read(_LORA_CHUNK_SIZE), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise WorkerError(
                "lora_hash_unavailable",
                f"не удалось прочитать LoRA {resolved}: {exc}",
            ) from exc
        return digest.hexdigest()

    return hash_lora


def _write_temp_output(data: bytes, filename: str) -> Path:
    """Записать видео во временный файл для ffprobe (в finally удаляется)."""

    suffix = Path(filename).suffix if filename else ".mp4"
    try:
        fd, temp_name = tempfile.mkstemp(prefix="h3-output-", suffix=suffix or ".mp4")
    except OSError as exc:
        raise WorkerError("media_probe_failed", f"не удалось создать temp-файл для ffprobe: {exc}") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except OSError as exc:
        Path(temp_name).unlink(missing_ok=True)
        raise WorkerError("media_probe_failed", f"не удалось записать temp-файл для ffprobe: {exc}") from exc
    return Path(temp_name)


async def execute(job: Any, deps: Dependencies) -> dict[str, Any]:
    """Выполнить один job; ошибки — ``WorkerError`` наружу (job FAILED)."""

    payload = _validate_job(job)
    action = payload.get("action")
    if action == "health":
        # Health не трогает assets и не грузит модель (`contract/worker-contract.md` §3).
        _LOG.info("job %s: health, ComfyUI/граф/assets не трогаем", _job_id(job))
        return await health_payload(deps.config, deps.comfy)
    if action is not None:
        raise WorkerError("input_invalid", f"input.action: неизвестное значение {action!r}")

    prompt = payload.get("prompt")
    if not isinstance(prompt, Mapping) or not prompt:
        raise WorkerError(
            "input_prompt_missing",
            "input.prompt отсутствует или пуст: без action='health' нужен готовый API-граф",
        )
    parameter = _optional_mapping(payload, "parameter")
    acceleration = _optional_mapping(payload, "acceleration")

    comfy = deps.comfy
    config = deps.config
    # Структура/заявленные размеры assets проверяются до ожидания ComfyUI:
    # ошибка контракта видна сразу, а не после холодного старта
    # (`contract/worker-contract.md` §3).
    assets = validate_assets(payload.get("assets", []), config)

    # ComfyUI может стартовать дольше endpoint'а — ждём до COMFY_BOOT_TIMEOUT_SECONDS.
    await comfy.wait_ready(config.comfy_boot_timeout_seconds)
    # Assets — до submit: граф ссылается на имена файлов в ComfyUI/input.
    asset_paths = await deps.assets_downloader(assets, config)
    if asset_paths:
        _LOG.info("job %s: доставлено assets=%d", _job_id(job), len(asset_paths))

    started = time.monotonic()
    prompt_id = await comfy.submit(dict(prompt))
    _LOG.info("job %s: граф отправлен, prompt_id=%s", _job_id(job), prompt_id)

    try:
        history_entry = await comfy.wait(prompt_id, config.job_timeout_seconds)
    except asyncio.CancelledError:
        # Отмена job должна гасить текущий граф в ComfyUI, затем проброс.
        _LOG.warning("job %s: отмена, прерываю ComfyUI (prompt_id=%s)", _job_id(job), prompt_id)
        await comfy.interrupt()
        raise
    # Фактические секунды GPU: submit → history completed.
    elapsed_seconds = time.monotonic() - started

    output = await comfy.fetch_output(history_entry)
    temp_path: Path | None = None
    try:
        temp_path = _write_temp_output(output.data, output.filename)
        media = await deps.media_prober(
            temp_path,
            config.ffprobe_bin,
            config.media_probe_timeout_seconds,
        )
        lora_hasher = deps.lora_hasher or make_lora_hasher(str(config.models_root))
        telemetry = build_telemetry(
            prompt,
            parameter,
            acceleration,
            history_entry=history_entry,
            media=media,
            elapsed_seconds=elapsed_seconds,
            lora_hash_fn=lora_hasher,
            cache="miss",
        )
        delivery = await deliver_output(output.data, output.filename, config, deps.storage)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

    result: dict[str, Any] = {**delivery, "telemetry": telemetry}
    _LOG.info(
        "job %s: готово, %s байт, доставка=%s",
        _job_id(job),
        len(output.data),
        "video_url" if "video_url" in delivery else "inline base64",
    )
    return result


def build_handler(
    config: Config,
    comfy: ComfyClient,
    storage: OutputStorage | None = None,
    *,
    assets_downloader: AssetsDownloader = download_assets,
    media_prober: MediaProber = probe_media,
    lora_hasher: LoraHasher | None = None,
) -> "Any":
    """Собрать handler с зафиксированными зависимостями (для тестов и main)."""

    deps = Dependencies(
        config=config,
        comfy=comfy,
        storage=storage,
        assets_downloader=assets_downloader,
        media_prober=media_prober,
        lora_hasher=lora_hasher,
    )

    async def handler(job: Any) -> dict[str, Any]:
        return await execute(job, deps)

    return handler


# Ленивый дефолт для RunPod: config/comfy создаются при первом job, чтобы
# импорт модуля (и тесты) не требовали живого ComfyUI.
_default_deps: Dependencies | None = None


def default_dependencies() -> Dependencies:
    """Зависимости по умолчанию из env (`Config.from_env`, локальный ComfyUI)."""

    global _default_deps
    if _default_deps is None:
        config = Config.from_env()
        _default_deps = Dependencies(config=config, comfy=ComfyClient(config))
    return _default_deps


async def handler(job: Any) -> dict[str, Any]:
    """Точка входа RunPod (`main.py`); тесты подменяют ``_default_deps``."""

    return await execute(job, default_dependencies())


__all__ = [
    "Dependencies",
    "build_handler",
    "default_dependencies",
    "execute",
    "handler",
    "make_lora_hasher",
]
