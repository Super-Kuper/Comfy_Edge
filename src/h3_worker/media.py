"""Измерение готового видео через ffprobe для телеметрии v1.1.

Контракт: `contract/worker-contract.md` §2/§5.
Источник фактов — сам mp4, а не снимок `parameter`: ``visible_frames``,
``audio``, ``orientation`` (по width/height) собираются из результата ffprobe.
Ошибки/таймаут/отсутствие видео-потока — видимая ошибка
``media_probe_failed`` (job FAILED), без подстановки ожидаемых значений.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DEFAULT_FFPROBE_BIN
from .errors import WorkerError

_LOG = logging.getLogger(__name__)

# `ffprobe -v error -print_format json -show_format -show_streams`: только JSON
# в stdout, без прогресса и предупреждений в разборе.
_FFPROBE_ARGS = ("-v", "error", "-print_format", "json", "-show_format", "-show_streams")
_STDERR_TAIL = 500


@dataclass(frozen=True)
class MediaInfo:
    """Факты готового mp4."""

    duration_seconds: float
    frames: int
    has_audio: bool
    width: int
    height: int
    fps: float


def _probe_error(detail: str) -> WorkerError:
    return WorkerError("media_probe_failed", detail)


def _as_float(value: Any) -> float | None:
    """Число из ffprobe (int/float/str), иначе ``None`` (``N/A`` — не факт)."""

    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):  # NaN/∞
        return None
    return number


def _as_int(value: Any) -> int | None:
    """Целое из ffprobe; float принимается только целочисленный."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    number = _as_float(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _parse_rate(value: Any) -> float | None:
    """Разобрать ``avg_frame_rate``/``r_frame_rate`` вида ``"24/1"``/``"30000/1001"``."""

    if isinstance(value, str) and "/" in value:
        numerator_raw, _, denominator_raw = value.partition("/")
        numerator = _as_float(numerator_raw)
        denominator = _as_float(denominator_raw)
        if numerator is None or denominator is None or denominator == 0:
            return None
        rate = numerator / denominator
    else:
        rate = _as_float(value)
    if rate is None or rate <= 0:
        return None
    return rate


def _is_attached_picture(stream: Mapping[str, Any]) -> bool:
    """Обложка/id3-картинка не считается видео-потоком."""

    disposition = stream.get("disposition")
    return isinstance(disposition, Mapping) and bool(disposition.get("attached_pic"))


def parse_ffprobe_json(data: Mapping[str, Any]) -> MediaInfo:
    """Превратить JSON ``ffprobe`` в ``MediaInfo`` (без запуска процесса).

    - `frames` — ``nb_frames`` видео-потока, иначе ``round(duration * fps)``;
    - `fps` — ``avg_frame_rate`` (запасной — ``r_frame_rate``), дробь вида
      ``"24/1"``; если поток не сообщает rate, считаем ``frames / duration``;
    - `has_audio` — есть ли аудио-поток;
    - ширина/высота — из видео-потока (0, если ffprobe их не сообщил);
    - нет видео-потока / не удалось определить кадры-длительность →
      ``WorkerError("media_probe_failed")``.
    """

    if not isinstance(data, Mapping):
        raise _probe_error("ffprobe: ответ не является JSON-объектом")
    streams = data.get("streams")
    if not isinstance(streams, list):
        raise _probe_error("ffprobe: в ответе нет списка streams")

    video_stream: Mapping[str, Any] | None = None
    has_audio = False
    for stream in streams:
        if not isinstance(stream, Mapping):
            continue
        codec_type = stream.get("codec_type")
        if codec_type == "audio":
            has_audio = True
        elif codec_type == "video" and not _is_attached_picture(stream) and video_stream is None:
            video_stream = stream
    if video_stream is None:
        raise _probe_error("ffprobe: в файле нет видео-потока (аудио не считается результатом)")

    fps = _parse_rate(video_stream.get("avg_frame_rate")) or _parse_rate(video_stream.get("r_frame_rate"))

    format_section = data.get("format")
    format_duration = _as_float(format_section.get("duration")) if isinstance(format_section, Mapping) else None
    duration = format_duration or _as_float(video_stream.get("duration"))

    frames = _as_int(video_stream.get("nb_frames"))
    if frames is None or frames <= 0:
        if duration is not None and duration > 0 and fps is not None and fps > 0:
            frames = int(round(duration * fps))
    if frames is None or frames <= 0:
        raise _probe_error("ffprobe: не удалось определить число кадров (нет nb_frames и нет пары duration+fps)")

    if fps is None or fps <= 0:
        fps = frames / duration if duration is not None and duration > 0 else 0.0
    if duration is None or duration <= 0:
        duration = frames / fps if fps > 0 else 0.0
    if duration <= 0:
        raise _probe_error("ffprobe: не удалось определить длительность видео")

    return MediaInfo(
        duration_seconds=float(duration),
        frames=int(frames),
        has_audio=has_audio,
        width=max(0, _as_int(video_stream.get("width")) or 0),
        height=max(0, _as_int(video_stream.get("height")) or 0),
        fps=float(fps),
    )


async def probe_media(
    path: str | Path,
    ffprobe_bin: str = DEFAULT_FFPROBE_BIN,
    timeout: float = 30.0,
) -> MediaInfo:
    """Запустить ``ffprobe`` на файле и вернуть ``MediaInfo``.

    Ошибки процесса (нет бинаря, ненулевой код, таймаут, не-JSON) — видимая
    ошибка ``media_probe_failed``; ffprobe-процесс при таймауте убивается.
    """

    args = [str(ffprobe_bin), *_FFPROBE_ARGS, str(path)]
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise _probe_error(f"ffprobe не найден: {ffprobe_bin!r}") from exc
    except OSError as exc:
        raise _probe_error(f"не удалось запустить ffprobe {ffprobe_bin!r}: {exc}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError as exc:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        # Дожидаемся reaping, чтобы не оставлять зомби/висящий transport.
        try:
            await process.wait()
        except Exception:  # noqa: BLE001 - best effort, ошибка уже есть
            pass
        raise _probe_error(f"ffprobe не уложился в {timeout:g} с: {path}") from exc
    except asyncio.CancelledError:
        # Отмена job: не оставляем живой ffprobe; пробрасываем CancelledError.
        try:
            process.kill()
        except ProcessLookupError:
            pass
        raise

    if process.returncode != 0:
        tail = (stderr or b"").decode("utf-8", errors="replace").strip()[:_STDERR_TAIL]
        raise _probe_error(f"ffprobe вернул код {process.returncode}: {tail or 'без stderr'}")
    try:
        data = json.loads((stdout or b"").decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise _probe_error(f"ffprobe вернул не-JSON: {path}") from exc
    try:
        return parse_ffprobe_json(data)
    except WorkerError:
        raise
    except Exception as exc:  # noqa: BLE001 - защита от неожиданной структуры
        raise _probe_error(f"ffprobe: не удалось разобрать ответ: {type(exc).__name__}: {exc}") from exc


__all__ = ["MediaInfo", "parse_ffprobe_json", "probe_media"]
