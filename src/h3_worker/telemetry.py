"""Телеметрия факта v1.1: исполненный граф + медиа + замер.

Источник истины — **факты исполнения**, собираемые воркером
(`contract/worker-contract.md` §2):

- `gpu_cc`/`gpu_class` — ``torch.cuda`` (или env-override `H3_GPU_CC/H3_GPU_CLASS`);
- `seconds` — замер handler'а (submit → history completed);
- `visible_frames`/`audio`/`orientation` — ``ffprobe`` готового mp4 (``media``);
- `steps`/`mode`/`lora_name`/`sol_called`/`pip`/`continuation_*` — по фактически
  исполненному графу `prompt` (API-format ComfyUI);
- `lora_hash` — sha256 файла LoRA с тома (`lora_hash_fn`);
- `cache` — ``"hit"``/``"miss"`` (кэш CLIPCached сейчас не задействован → ``"miss"``).

Если в ``history["telemetry"]`` уже есть телеметрия, она
проходит как есть, воркер лишь дозаполняет отсутствующие ``gpu_cc``/``gpu_class``.
Ожидаемые значения из `parameter`/`acceleration` **не подставляются**: при
невозможности измерить факт — видимая ошибка ``telemetry_missing``, при
расхождении шагов сэмплеров — ``telemetry_ambiguous``.
"""

from __future__ import annotations

import copy
import math
import os
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from .errors import WorkerError
from .media import MediaInfo

# Обязательный минимум телеметрии (`contract/output.schema.json`).
# Нужны и для сверки телеметрии с ожиданиями приложения.
_TELEMETRY_GPU_CC = "gpu_cc"
_TELEMETRY_GPU_CLASS = "gpu_class"

_KNOWN_GPU_CLASSES = ("RTX PRO 6000", "RTX 5090")

# Классы нод исполненного графа.
_CONDITIONING_CLASSES = ("MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo")
_SOL_CLASSES = ("SolH3Exact", "SolH3Experimental")
_LORA_CLASSES = ("LoraLoaderModelOnly", "LoraLoader")
_CONTINUATION_CLASS = "MiniMaxH3GuidedContinuationWindow"
_SAMPLER_CLASS = "SamplerCustomAdvanced"
_SCHEDULER_CLASS = "BasicScheduler"
_KSAMPLER_CLASSES = ("KSampler", "KSamplerAdvanced")

LoraHashFn = Callable[[str], str | None]


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _torch_gpu() -> tuple[float, str]:
    """Фактические cc/class из torch.cuda (ленивый импорт — тесты без torch)."""

    try:
        import torch  # noqa: PLC0415 - намеренно ленивый импорт
    except Exception as exc:  # noqa: BLE001 - нет torch/сломанное окружение
        raise WorkerError(
            "gpu_unavailable",
            f"torch недоступен, а H3_GPU_CC/H3_GPU_CLASS не заданы: {type(exc).__name__}: {exc}",
        ) from exc

    cuda = getattr(torch, "cuda", None)
    try:
        available = bool(cuda is not None and cuda.is_available())
    except Exception as exc:  # noqa: BLE001 - CUDA может бросить на старте
        raise WorkerError("gpu_unavailable", f"torch.cuda.is_available() упал: {exc}") from exc
    if not available:
        raise WorkerError("gpu_unavailable", "CUDA недоступна: torch.cuda.is_available() = False")

    try:
        major, minor = cuda.get_device_capability(0)
        name = str(cuda.get_device_name(0))
    except Exception as exc:  # noqa: BLE001
        raise WorkerError("gpu_unavailable", f"не удалось прочитать GPU через torch.cuda: {exc}") from exc
    return float(f"{int(major)}.{int(minor)}"), _gpu_class_from_name(name)


def _gpu_class_from_name(name: str) -> str:
    """Класс GPU по имени устройства; неизвестное имя возвращаем как факт."""

    upper = name.upper()
    for gpu_class in _KNOWN_GPU_CLASSES:
        if gpu_class in upper:
            return gpu_class
    return name


def detect_gpu(env: Mapping[str, str] | None = None) -> tuple[float, str]:
    """Вернуть фактическую пару ``(gpu_cc, gpu_class)``.

    Приоритет: явный override ``H3_GPU_CC``/``H3_GPU_CLASS`` (окружения без
    torch/для E2E-диагностики) → ``torch.cuda`` (capability + имя). Недоступно →
    ``WorkerError(code="gpu_unavailable")``; выдумывать класс GPU нельзя.
    """

    source: Mapping[str, str] = os.environ if env is None else env
    cc_raw = _clean(source.get("H3_GPU_CC"))
    class_raw = _clean(source.get("H3_GPU_CLASS"))
    if cc_raw is not None or class_raw is not None:
        if cc_raw is None or class_raw is None:
            raise WorkerError(
                "config_invalid",
                "H3_GPU_CC и H3_GPU_CLASS должны задаваться вместе",
            )
        try:
            cc = float(cc_raw)
        except ValueError as exc:
            raise WorkerError(
                "config_invalid",
                f"H3_GPU_CC: ожидалось число, получено {cc_raw!r}",
            ) from exc
        if not math.isfinite(cc) or cc <= 0:
            raise WorkerError("config_invalid", f"H3_GPU_CC: ожидалось положительное число, получено {cc_raw!r}")
        return cc, class_raw
    return _torch_gpu()


# ---------------------------------------------------------------------------
# Обязательные поля и проверки типов (`contract/output.schema.json`).
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_cache(value: Any) -> bool:
    return isinstance(value, str) and value in {"hit", "miss"}


_REQUIRED_FIELDS: tuple[tuple[str, Any], ...] = (
    (_TELEMETRY_GPU_CC, _is_number),
    (_TELEMETRY_GPU_CLASS, _is_str),
    ("sol_called", _is_bool),
    ("steps", _is_integer),
    ("orientation", _is_str),
    ("visible_frames", _is_integer),
    ("audio", _is_bool),
    ("cache", _is_cache),
    ("seconds", _is_number),
)

_REQUIRED_NAMES = tuple(name for name, _ in _REQUIRED_FIELDS)


def _validate_required(telemetry: Mapping[str, Any], source: str) -> None:
    """Проверить обязательные поля/типы; нарушения — ``telemetry_missing``."""

    problems: list[str] = []
    for name, check in _REQUIRED_FIELDS:
        if name not in telemetry or telemetry[name] is None:
            problems.append(name)
        elif not check(telemetry[name]):
            problems.append(f"{name} (неверный тип/значение)")
    if problems:
        raise WorkerError(
            "telemetry_missing",
            f"{source}: отсутствуют или некорректны обязательные поля: "
            + ", ".join(problems)
            + "; ожидаемые значения не подставляются",
        )


# ---------------------------------------------------------------------------
# Passthrough history["telemetry"] (совместимость с готовой телеметрией).
# ---------------------------------------------------------------------------


def _passthrough_telemetry(
    history_entry: Mapping[str, Any] | None,
    gpu: tuple[float, str] | None,
) -> dict[str, Any] | None:
    """Вернуть копию ``history["telemetry"]`` или ``None``, если её нет.

    Воркер дозаполняет только отсутствующие ``gpu_cc``/``gpu_class`` — остальные
    поля уже являются фактом узлов.
    """

    if not isinstance(history_entry, Mapping):
        return None
    raw = history_entry.get("telemetry")
    if not isinstance(raw, Mapping) or not raw:
        return None

    telemetry: dict[str, Any] = copy.deepcopy(dict(raw))
    missing_gpu = telemetry.get(_TELEMETRY_GPU_CC) is None or telemetry.get(_TELEMETRY_GPU_CLASS) is None
    if missing_gpu:
        cc, gpu_class = gpu if gpu is not None else detect_gpu()
        if telemetry.get(_TELEMETRY_GPU_CC) is None:
            telemetry[_TELEMETRY_GPU_CC] = cc
        if telemetry.get(_TELEMETRY_GPU_CLASS) is None:
            telemetry[_TELEMETRY_GPU_CLASS] = gpu_class

    _validate_required(telemetry, "history.telemetry")
    return telemetry


# ---------------------------------------------------------------------------
# Факты исполненного графа (API-format ComfyUI: dict id → {class_type, inputs}).
# ---------------------------------------------------------------------------


def _iter_nodes(prompt: Any) -> Iterator[tuple[str, Mapping[str, Any]]]:
    """Узлы графа в порядке графа: ``(id, {class_type, inputs})``."""

    if not isinstance(prompt, Mapping):
        return
    for node_id, node in prompt.items():
        if isinstance(node, Mapping):
            yield str(node_id), node


def _node_inputs(node: Mapping[str, Any]) -> Mapping[str, Any]:
    inputs = node.get("inputs")
    return inputs if isinstance(inputs, Mapping) else {}


def _node_class(node: Mapping[str, Any]) -> str | None:
    class_type = node.get("class_type")
    return class_type if isinstance(class_type, str) else None


def _class_types(prompt: Any) -> set[str]:
    return {_node_class(node) for _, node in _iter_nodes(prompt)} - {None}


def _link_target(value: Any) -> str | None:
    """Цель связи ComfyUI ``[node_id, slot]`` → ``str(node_id)``."""

    if isinstance(value, (list, tuple)) and len(value) == 2 and not isinstance(value[0], bool):
        return str(value[0])
    return None


def _int_value(value: Any) -> int | None:
    """Целое из inputs графа (int или целочисленный float), иначе ``None``."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _steps_from_graph(prompt: Any) -> int:
    """Шаги из фактического графа; расхождение → ``telemetry_ambiguous``.

    ``SamplerCustomAdvanced`` берёт шаги из связанного по `sigmas`
    ``BasicScheduler``; также учитываются
    нативные ``KSampler``/``KSamplerAdvanced`` с прямым `inputs.steps`.
    """

    nodes = dict(_iter_nodes(prompt))
    found: list[tuple[str, int]] = []
    for node_id, node in nodes.items():
        class_type = _node_class(node)
        inputs = _node_inputs(node)
        if class_type in _KSAMPLER_CLASSES:
            if "steps" in inputs:
                found.append((f"{class_type} {node_id}", _step_value(inputs["steps"], class_type, node_id)))
        elif class_type == _SAMPLER_CLASS:
            target = _link_target(inputs.get("sigmas"))
            if target is None:
                continue
            scheduler = nodes.get(target)
            if scheduler is None or _node_class(scheduler) != _SCHEDULER_CLASS:
                continue
            scheduler_inputs = _node_inputs(scheduler)
            if "steps" in scheduler_inputs:
                found.append(
                    (
                        f"{_SCHEDULER_CLASS} {target} (sigmas {_SAMPLER_CLASS} {node_id})",
                        _step_value(scheduler_inputs["steps"], _SCHEDULER_CLASS, target),
                    )
                )
    if not found:
        raise WorkerError(
            "telemetry_missing",
            f"факты исполнения: в графе нет шагов сэмплера ({_SAMPLER_CLASS}→{_SCHEDULER_CLASS} или KSampler.steps)",
        )
    values = sorted({value for _, value in found})
    if len(values) > 1:
        detail = "; ".join(f"{where}={value}" for where, value in found)
        raise WorkerError(
            "telemetry_ambiguous",
            f"шаги сэмплеров в исполненном графе расходятся: {detail}",
        )
    return values[0]


def _step_value(value: Any, class_type: str, node_id: str) -> int:
    steps = _int_value(value)
    if steps is None or steps <= 0:
        raise WorkerError(
            "telemetry_missing",
            f"факты исполнения: {class_type} {node_id}.steps={value!r} — ожидалось целое > 0",
        )
    return steps


def _orientation_from_size(width: Any, height: Any) -> str | None:
    """Ориентация по фактическим ширине/высоте (`h>w` → 9:16, `w>h` → 16:9)."""

    w = _int_value(width)
    h = _int_value(height)
    if w is None or h is None or w <= 0 or h <= 0:
        return None
    if h > w:
        return "9:16"
    if w > h:
        return "16:9"
    return None


def _orientation_from_graph(prompt: Any) -> str | None:
    """Запасной источник ориентации — width/height conditioning-ноды графа."""

    for _, node in _iter_nodes(prompt):
        if _node_class(node) not in _CONDITIONING_CLASSES:
            continue
        inputs = _node_inputs(node)
        orientation = _orientation_from_size(inputs.get("width"), inputs.get("height"))
        if orientation is not None:
            return orientation
    return None


def _mode_from_graph(prompt: Any) -> str | None:
    """Режим по типу фактической conditioning-ноды."""

    classes = _class_types(prompt)
    if "MiniMaxH3ReferenceToVideo" in classes:
        return "Ref2VA"
    if "MiniMaxH3ImageToVideo" in classes:
        return "FL2VA"
    return None


def _visible_frames_from_graph(prompt: Any, continuation_count: int, extension: int | None) -> int | None:
    """Запасной расчёт кадров: `length` conditioning-ноды + count*extension."""

    for _, node in _iter_nodes(prompt):
        if _node_class(node) not in _CONDITIONING_CLASSES:
            continue
        length = _int_value(_node_inputs(node).get("length"))
        if length is None or length <= 0:
            continue
        total = length
        if continuation_count > 0 and extension is not None and extension > 0:
            total += continuation_count * extension
        return total
    return None


def _first_lora(prompt: Any) -> tuple[bool, str | None]:
    """Первый ``LoraLoader*`` графа → ``(узел_есть, lora_name)``."""

    for _, node in _iter_nodes(prompt):
        if _node_class(node) in _LORA_CLASSES:
            name = _node_inputs(node).get("lora_name")
            return True, name if isinstance(name, str) and name.strip() else None
    return False, None


def _continuation_from_graph(prompt: Any) -> dict[str, Any]:
    """Поля продолжения по фактическим continuation-нодам."""

    windows: list[Mapping[str, Any]] = []
    image_to_video: list[Mapping[str, Any]] = []
    for _, node in _iter_nodes(prompt):
        class_type = _node_class(node)
        if class_type == _CONTINUATION_CLASS:
            windows.append(node)
        elif class_type == "MiniMaxH3ImageToVideo":
            image_to_video.append(node)

    count = len(windows)
    overlap: int | None = None
    extension: int | None = None
    if windows:
        inputs = _node_inputs(windows[0])
        overlap = _int_value(inputs.get("overlap_frames"))
        extension = _int_value(inputs.get("extension_frames"))
    window_length = overlap + extension if overlap is not None and extension is not None else None
    append_frames = count * extension if extension is not None else None

    # Продолжения — дополнительные MiniMaxH3ImageToVideo сверх базовой
    # conditioning-ноды: для FL2VA базовой считается первая ImageToVideo;
    # для Ref2VA базовой является ReferenceToVideo, поэтому все ImageToVideo —
    # расширения.
    mode = _mode_from_graph(prompt)
    extension_nodes = image_to_video[1:] if mode != "Ref2VA" else image_to_video
    first_frame_connected = any(_input_connected(node, "first_frame") for node in extension_nodes)
    last_frame_connected = any(_input_connected(node, "last_frame") for node in extension_nodes)

    return {
        "continuation_nodes": count > 0,
        "window_length": window_length,
        "overlap_frames": overlap,
        "extension_frames": extension,
        "append_frames": append_frames,
        "latent_tail_broken": False,
        "first_frame_connected": first_frame_connected,
        "last_frame_connected": last_frame_connected,
        "_count": count,
    }


def _input_connected(node: Mapping[str, Any], name: str) -> bool:
    """Подключён ли вход ноды (связь ``[id, slot]`` или непустое значение)."""

    inputs = _node_inputs(node)
    if name not in inputs:
        return False
    value = inputs[name]
    if value is None:
        return False
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return True
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


# ---------------------------------------------------------------------------
# Сборка телеметрии.
# ---------------------------------------------------------------------------


def build_telemetry(
    prompt: Mapping[str, Any] | None,
    parameter: Mapping[str, Any] | None = None,
    acceleration: Mapping[str, Any] | None = None,
    *,
    history_entry: Mapping[str, Any] | None = None,
    media: MediaInfo | None = None,
    gpu: tuple[float, str] | None = None,
    elapsed_seconds: float | None = None,
    lora_hash_fn: LoraHashFn | None = None,
    cache: str | None = None,
) -> dict[str, Any]:
    """Собрать телеметрию ответа: passthrough history или факты исполнения.

    ``parameter``/``acceleration`` — снимок приложения; приняты для единой
    подписи и **не** используются для подстановки ожидаемых значений.
    Факты берутся из исполненного `prompt`, `media` (ffprobe),
    `elapsed_seconds`, `gpu` и `lora_hash_fn`.

    Ветки:
    1. ``history_entry["telemetry"]`` — непустой dict → passthrough + дозаполнение
       gpu (совместимость с готовой телеметрией из history);
    2. иначе — телеметрия из фактов; без измеримых фактов — ``telemetry_missing``,
       расхождение шагов — ``telemetry_ambiguous``.
    """

    passthrough = _passthrough_telemetry(history_entry, gpu)
    if passthrough is not None:
        return passthrough

    if not isinstance(prompt, Mapping) or not prompt:
        raise WorkerError(
            "telemetry_missing",
            "факты исполнения: prompt (исполненный граф) отсутствует или пуст",
        )

    if media is None:
        raise WorkerError(
            "telemetry_missing",
            "факты исполнения: media/ffprobe отсутствует — audio/visible_frames/"
            "orientation не измерить (ожидаемые значения не подставляются)",
        )
    frames = getattr(media, "frames", None)
    has_audio = getattr(media, "has_audio", None)
    if not _is_bool(has_audio):
        raise WorkerError("telemetry_missing", "факты исполнения: media.has_audio — ожидался bool")

    cc, gpu_class = gpu if gpu is not None else detect_gpu()
    if not _is_number(cc) or float(cc) <= 0 or not _is_str(gpu_class):
        raise WorkerError(
            "telemetry_missing",
            f"факты исполнения: gpu=({cc!r}, {gpu_class!r}) — ожидалась пара (cc>0, имя)",
        )

    if elapsed_seconds is None or not _is_number(elapsed_seconds) or float(elapsed_seconds) <= 0:
        raise WorkerError(
            "telemetry_missing",
            f"факты исполнения: elapsed_seconds={elapsed_seconds!r} — замер submit→history "
            "completed не выполнен (seconds > 0)",
        )
    if cache is None:
        cache_value = "miss"
    elif _is_cache(cache):
        cache_value = cache
    else:
        raise WorkerError("telemetry_missing", f"факты исполнения: cache={cache!r} — ожидалось hit|miss")

    steps = _steps_from_graph(prompt)
    mode = _mode_from_graph(prompt)
    if mode is None:
        raise WorkerError(
            "telemetry_missing",
            "факты исполнения: в графе нет MiniMaxH3ImageToVideo/"
            "MiniMaxH3ReferenceToVideo — mode не определить (ожидаемые значения не подставляются)",
        )

    orientation = _orientation_from_size(getattr(media, "width", None), getattr(media, "height", None))
    if orientation is None:
        orientation = _orientation_from_graph(prompt)
    if orientation is None:
        raise WorkerError(
            "telemetry_missing",
            "факты исполнения: размер видео и conditioning-ноды не дают ориентацию "
            "(ожидалось h>w → 9:16 или w>h → 16:9)",
        )

    continuation = _continuation_from_graph(prompt)
    continuation_count = int(continuation.pop("_count"))
    # visible_frames — измеренные кадры mp4; запасной расчёт по графу
    # (`length` conditioning-ноды + count*extension) — только если ffprobe не дал.
    visible_frames: int | None = int(frames) if _is_integer(frames) and frames > 0 else None
    if visible_frames is None:
        visible_frames = _visible_frames_from_graph(prompt, continuation_count, continuation["extension_frames"])
    if visible_frames is None or visible_frames <= 0:
        raise WorkerError(
            "telemetry_missing",
            "факты исполнения: visible_frames не измерить (media.frames и length графа пусты)",
        )

    classes = _class_types(prompt)
    sol_called = any(name in classes for name in _SOL_CLASSES)
    lora_present, lora_name = _first_lora(prompt)
    lora_hash: str | None = None
    if lora_present and lora_name is not None and lora_hash_fn is not None:
        # Ошибки hasher'а (нет файла на томе) — видимые: lora_hash_unavailable.
        candidate = lora_hash_fn(lora_name)
        lora_hash = candidate if isinstance(candidate, str) and candidate.strip() else None

    telemetry: dict[str, Any] = {
        _TELEMETRY_GPU_CC: float(cc),
        _TELEMETRY_GPU_CLASS: str(gpu_class),
        "sol_called": bool(sol_called),
        "steps": steps,
        "orientation": orientation,
        "visible_frames": int(visible_frames),
        "audio": bool(has_audio),
        "cache": cache_value,
        "seconds": float(elapsed_seconds),
        "mode": mode,
        "lora_name": lora_name,
        "lora_hash": lora_hash,
        "lora_applied": bool(lora_present),
        "pip": any(class_type.startswith("PiP") or class_type.startswith("H3PiP") for class_type in classes),
        **continuation,
    }
    if sol_called:
        telemetry["sol_backend"] = "comfyui-sol-h3"

    _validate_required(telemetry, "факты исполнения")
    return telemetry


__all__ = ["LoraHashFn", "build_telemetry", "detect_gpu", "_REQUIRED_NAMES"]
