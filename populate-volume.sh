#!/usr/bin/env bash
# =============================================================================
# populate-volume.sh — скачивание моделей на сетевой том RunPod.
#
# Читает models-manifest.txt (tab-separated: <subdir> <filename> <URL>) и
# кладёт файлы в ${VOL_ROOT}/models/<subdir>/<filename> — это тот же layout,
# который extra_model_paths.yaml отдаёт ComfyUI. Скрипт идемпотентен: уже
# существующие файлы пропускаются. Конкурентные воркеры сериализуются flock'ом
# на ${VOL_ROOT}/.populate.lock, поэтому холодный старт нескольких воркеров на
# свежем томе не качает одни и те же файлы параллельно.
#
# Env:
#   VOL_ROOT     точка монтирования тома      (по умолчанию /runpod-volume)
#   MANIFEST     путь к манифесту моделей     (по умолчанию рядом со скриптом)
#   HF_TOKEN     токен Hugging Face для gated-репозиториев (опционально)
#   HF_ENDPOINT  зеркало Hugging Face         (по умолчанию https://huggingface.co)
#
# Вызывается из docker/entrypoint.sh при VOLUME_AUTOPOPULATE=true; при ошибке
# скачивания воркер продолжает старт, а job'ы падают с видимой ошибкой
# отсутствующих моделей (а не «тихо» работают на других весах).
# =============================================================================

set -uo pipefail

VOL_ROOT="${VOL_ROOT:-/runpod-volume}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="${MANIFEST:-${SCRIPT_DIR}/models-manifest.txt}"
HF_ENDPOINT="${HF_ENDPOINT:-https://huggingface.co}"
MODELS_DIR="${VOL_ROOT}/models"

log() { printf '[populate] %s\n' "$*"; }
warn() { printf '[populate] WARN: %s\n' "$*" >&2; }
die() {
    printf '[populate] ERROR: %s\n' "$*" >&2
    exit 1
}

if [[ ! -d "${VOL_ROOT}" ]]; then
    log "том ${VOL_ROOT} не смонтирован — скачивание пропущено (attach network volume к endpoint'у)"
    exit 0
fi
if [[ ! -f "${MANIFEST}" ]]; then
    die "нет манифеста ${MANIFEST}: образ собран некорректно (ожидался COPY models-manifest.txt)"
fi

# Сериализация конкурентных воркеров на общем томе (9 — произвольный fd).
if command -v flock >/dev/null 2>&1; then
    exec 9>"${VOL_ROOT}/.populate.lock"
    flock 9
else
    warn "flock не найден — параллельный populate не сериализуется"
fi

total="$(grep -vcE '^[[:space:]]*(#|$)' "${MANIFEST}" || true)"
log "манифест: ${total} файлов; свободно на ${VOL_ROOT}: $(df -h "${VOL_ROOT}" 2>/dev/null | awk 'NR==2{print $4}')"
mkdir -p "${MODELS_DIR}"

done_n=0
failed=0
while IFS=$'\t' read -r sub name url; do
    sub="${sub%$'\r'}"
    name="${name%$'\r'}"
    url="${url%$'\r'}"
    case "${sub}" in ''|\#*) continue ;; esac
    done_n=$((done_n + 1))

    if [[ -z "${name}" || -z "${url}" ]]; then
        warn "строка ${done_n}: ожидались <subdir> <filename> <URL> (получено: ${sub})"
        failed=$((failed + 1))
        continue
    fi

    dest="${MODELS_DIR}/${sub}/${name}"
    if [[ -s "${dest}" ]]; then
        log "[${done_n}/${total}] есть ${sub}/${name}"
        continue
    fi

    # Зеркало Hugging Face: подменяем только префикс, путь/ревизия сохраняются.
    if [[ "${HF_ENDPOINT}" != "https://huggingface.co" && "${url}" == https://huggingface.co/* ]]; then
        url="${HF_ENDPOINT%/}/${url#https://huggingface.co/}"
    fi

    mkdir -p "${MODELS_DIR}/${sub}"
    log "[${done_n}/${total}] качаю ${sub}/${name}"
    hdr=()
    if [[ -n "${HF_TOKEN:-}" ]]; then
        hdr=(--header "Authorization: Bearer ${HF_TOKEN}")
    fi

    # --continue-at -: докачиваем .tmp после обрыва, не начиная файл заново.
    if curl --fail --location --silent --show-error \
        --retry 5 --retry-delay 10 --continue-at - \
        "${hdr[@]}" --output "${dest}.tmp" "${url}"; then
        mv -f "${dest}.tmp" "${dest}"
        log "[${done_n}/${total}] готово ${sub}/${name}"
    else
        code=$?
        warn "не удалось скачать ${sub}/${name} (curl exit ${code}); частичный файл: ${dest}.tmp"
        failed=$((failed + 1))
    fi
done <"${MANIFEST}"

if [[ "${failed}" -gt 0 ]]; then
    warn "не скачано файлов: ${failed} из ${total}; job'ы на отсутствующих моделях упадут с видимой ошибкой"
    exit 1
fi

log "готово: модели на ${MODELS_DIR} ($(df -h "${VOL_ROOT}" 2>/dev/null | awk 'NR==2{print $4}') свободно)"
