#!/usr/bin/env bash
# =============================================================================
# Подготовка ComfyUI v0.44.0 и публичных custom_nodes по пинам в образе
# serverless-воркера.
#
# ВАЖНО: официальный runpod/comfyui — «baked»-образ: готовый ComfyUI лежит в
# /opt/comfyui-baked, а /start.sh раскладывает его в /workspace/runpod-slim при
# старте пода (и создаёт venv с --system-site-packages, SSH/Jupyter/FileBrowser,
# ComfyUI на 0.0.0.0). Для serverless это неприемлемо (долгий сетевой bootstrap,
# лишние сервисы). Поэтому раскладка **материализуется на этапе сборки**:
#   /opt/comfyui-baked/            →  $COMFYUI_DIR/
#   venv                           →  $COMFYUI_PYTHON (--system-site-packages,
#                                     torch/CUDA из системных пакетов базы)
#   custom_nodes по пинам          →  $COMFYUI_DIR/custom_nodes/
# Наш docker/entrypoint.sh стартует ComfyUI на 127.0.0.1:8188 и НЕ вызывает
# /start.sh базового образа.
#
# Скрипт вызывается из Dockerfile ПОСЛЕ COPY и ДО кода воркера. Env:
#   COMFYUI_DIR          каталог ComfyUI (раскладка материализуется сюда)
#   COMFYUI_PYTHON       интерпретатор venv (torch/CUDA из system-site-packages)
#   COMFYUI_BAKED_DIR    baked-копия ComfyUI в базовом образе
#   COMFYUI_VERSION      ожидаемый тег ComfyUI (v0.44.0)
#   SKIP_COMFY_PACKAGES  =1 — не ставить пакеты (stub-сборка на python:3.12-slim)
#
# Stub-сборка: в python:3.12-slim нет /opt/comfyui-baked → шаг пропускается с
# логом и кодом 0 (проверка структуры слоя воркера).
#
# Пакеты:
#   city96/ComfyUI-GGUF @ 6ea2651e (+ pip gguf)       — UnetLoaderGGUF/CLIPLoaderGGUF
#   xmarre/ComfyUI-Sol-H3 @ fe6a9618 (+ requirements) — SolH3Exact/SolH3Experimental
#   ttulttul/ComfyUI-Minimax-H3-Continuation @ e1768d5f — continuation-ноды
#   Mu5hr00moO/ComfyUI-MiniMaxH3-CLIPCached @ 507c1ecd — CLIP-кэш (в графе по
#                                                        умолчанию не используется)
# Ошибки установки pip-зависимостей Sol/CLIPCached не валят сборку (тяжёлые
# CUDA-пакеты), но печатаются в лог сборки.
# =============================================================================

set -euo pipefail

COMFYUI_DIR="${COMFYUI_DIR:-/workspace/runpod-slim/ComfyUI}"
COMFYUI_PYTHON="${COMFYUI_PYTHON:-/workspace/runpod-slim/.venv-cu130/bin/python}"
COMFYUI_BAKED_DIR="${COMFYUI_BAKED_DIR:-/opt/comfyui-baked}"
COMFYUI_VERSION="${COMFYUI_VERSION:-v0.44.0}"
SKIP_COMFY_PACKAGES="${SKIP_COMFY_PACKAGES:-0}"
# uv (если появится в образе) не должен писать в read-only HOME при сборке.
export UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/uv-cache}"

log() { printf '[packages] %s\n' "$*"; }
warn() { printf '[packages] WARN: %s\n' "$*" >&2; }
die() {
    printf '[packages] ERROR: %s\n' "$*" >&2
    exit 1
}

# Держим torch/vision/audio в версиях базового образа (cu130) при любых pip
# install: файл есть в официальном runpod/comfyui. См. /start.sh базового образа.
if [[ -f /opt/comfyui-runtime-constraints.txt ]]; then
    export PIP_CONSTRAINT=/opt/comfyui-runtime-constraints.txt
    export PIP_BUILD_CONSTRAINT=/opt/comfyui-runtime-constraints.txt
    log "PIP_CONSTRAINT=/opt/comfyui-runtime-constraints.txt ($(tr '\n' ' ' </opt/comfyui-runtime-constraints.txt))"
fi

# --- 0. Применимость --------------------------------------------------------
if [[ ! -d "${COMFYUI_BAKED_DIR}" ]]; then
    log "SKIP: ${COMFYUI_BAKED_DIR} нет в базовом образе (stub-сборка без ComfyUI) — пакеты не ставятся"
    exit 0
fi
if [[ "${SKIP_COMFY_PACKAGES}" == "1" ]]; then
    log "SKIP_COMFY_PACKAGES=1: установка ComfyUI/пакетов пропущена (проверка структуры образа)"
    exit 0
fi

# --- 1. Версия ComfyUI в базовом образе -------------------------------------
# Digest базового образа закреплён под ComfyUI v0.44.0; если тег уже совпадает
# — checkout НЕ делаем (ничего не меняем). Проверка версии остаётся в логе
# сборки: это доказательство, что раскладка соответствует заявленной версии. Официальный baked-образ может быть и без .git — тогда
# версию читаем из comfyui_version.py.
if [[ -d "${COMFYUI_BAKED_DIR}/.git" ]]; then
    current="$(git -C "${COMFYUI_BAKED_DIR}" describe --tags --always 2>/dev/null || echo unknown)"
    log "ComfyUI в базовом образе: ${current} (ожидается ${COMFYUI_VERSION})"
    if [[ "${current}" == "${COMFYUI_VERSION}" || "${current}" == "${COMFYUI_VERSION}-"* ]]; then
        log "ComfyUI: версия совпадает с ${COMFYUI_VERSION} — checkout не требуется"
    else
        warn "ComfyUI: версия ${current} != ${COMFYUI_VERSION} — пробую checkout тега"
        git -C "${COMFYUI_BAKED_DIR}" fetch --tags origin
        git -C "${COMFYUI_BAKED_DIR}" checkout --detach "${COMFYUI_VERSION}"
        after="$(git -C "${COMFYUI_BAKED_DIR}" describe --tags --always 2>/dev/null || echo unknown)"
        log "ComfyUI после checkout: ${after}"
        if [[ "${after}" != "${COMFYUI_VERSION}" && "${after}" != "${COMFYUI_VERSION}-"* ]]; then
            die "не удалось привести ComfyUI к ${COMFYUI_VERSION} (сейчас ${after}); base-образ не соответствует пину"
        fi
    fi
else
    version_file="${COMFYUI_BAKED_DIR}/comfyui_version.py"
    if [[ ! -f "${version_file}" ]]; then
        die "${COMFYUI_BAKED_DIR}: нет ни .git, ни comfyui_version.py — не могу проверить версию ComfyUI (ожидалась ${COMFYUI_VERSION})"
    fi
    current="$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*["'\'']\([^"'\'']*\)["'\''].*/\1/p' "${version_file}" | head -1)"
    log "ComfyUI в базовом образе (без .git): ${current:-unknown} из comfyui_version.py (ожидается ${COMFYUI_VERSION})"
    if [[ "${current}" != "${COMFYUI_VERSION#v}" ]]; then
        die "версия ComfyUI ${current:-unknown} != ${COMFYUI_VERSION}: base-образ не соответствует пину"
    fi
fi

# --- 2. Раскладка: baked → /workspace/runpod-slim/ComfyUI --------------------
# .git не копируем (20 МБ, для запуска не нужен; версия уже проверена и
# залогирована). Остальные файлы ComfyUI переносим как есть, включая baked
# custom_nodes (Manager/KJNodes/Civicomfy/RunpodDirect) — поведение базового
# образа сохраняется, а deps этих нод уже в system site-packages.
mkdir -p "$(dirname "${COMFYUI_DIR}")"
log "раскладка ComfyUI: ${COMFYUI_BAKED_DIR}/ → ${COMFYUI_DIR}/ (без .git)"
rsync -a --exclude='/.git' "${COMFYUI_BAKED_DIR}/" "${COMFYUI_DIR}/"

# --- 3. venv ComfyUI ---------------------------------------------------------
# --system-site-packages: torch 2.10.0+cu130 и CUDA-стек уже в системных
# пакетах базового образа (constraints /opt/comfyui-runtime-constraints.txt);
# --without-pip: pip берётся из system site-packages и ставит пакеты в venv
# (sys.prefix), т.е. не тянем второй pip. Так же делает /start.sh базового образа.
if [[ ! -x "${COMFYUI_PYTHON}" ]]; then
    log "создаю venv ${COMFYUI_PYTHON%/bin/python} (--system-site-packages --without-pip)"
    python3 -m venv --system-site-packages --without-pip "${COMFYUI_PYTHON%/bin/python}"
fi
[[ -x "${COMFYUI_PYTHON}" ]] || die "не удалось создать venv ${COMFYUI_PYTHON}"
log "Python ComfyUI: $("${COMFYUI_PYTHON}" -V 2>&1)"
log "torch из base: $("${COMFYUI_PYTHON}" -c 'import torch; print(torch.__version__, "cuda", torch.version.cuda)' 2>&1 || echo 'import torch FAILED')"

# pip выбранного интерпретатора (с --without-pip это системный pip, но установка
# идёт в venv). Ошибки pip в хелпере фатальны: без рабочего venv образ бесполезен.
_pip() {
    local py="$1"
    shift
    if "${py}" -m pip --version >/dev/null 2>&1; then
        "${py}" -m pip install --no-cache-dir "$@"
    elif command -v uv >/dev/null 2>&1; then
        uv pip install --python "${py}" "$@"
    else
        "${py}" -m ensurepip --upgrade
        "${py}" -m pip install --no-cache-dir "$@"
    fi
}

# --- 4. ffmpeg/ffprobe -------------------------------------------------------
# Официальный ComfyUI-образ уже содержит ffmpeg/ffprobe (ноды записи видео), а
# handler использует ffprobe для телеметрии. Не дублируем, если он есть.
if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
    log "ffmpeg/ffprobe уже есть в базовом образе: $(ffmpeg -version 2>/dev/null | head -1)"
else
    log "ffmpeg/ffprobe нет — ставлю apt-пакетом ffmpeg"
    apt-get update
    apt-get install -y --no-install-recommends ffmpeg
    rm -rf /var/lib/apt/lists/*
fi

# --- 5. Публичные пакеты custom_nodes по пинам ------------------------------
_custom_nodes="${COMFYUI_DIR}/custom_nodes"
mkdir -p "${_custom_nodes}"

# _pin_node URL DST SHA — shallow clone + фиксация на коммите. Если SHA нет в
# shallow-истории (fetch по SHA не удался), переклонируем полностью — так пин
# соблюдается у любого зеркала/форка.
_pin_node() {
    local url="$1"
    local dst="$2"
    local sha="$3"
    local name
    name="$(basename "${dst}")"

    if [[ -d "${dst}/.git" ]]; then
        log "${name}: уже есть в базовом образе — сверяю пин ${sha}"
    else
        log "${name}: git clone ${url}"
        git clone --depth 1 "${url}" "${dst}"
    fi
    # fetch по SHA GitHub поддерживает; если нет — commit может уже быть в
    # shallow HEAD (тогда checkout ниже сработает).
    git -C "${dst}" fetch --depth 1 origin "${sha}" >/dev/null 2>&1 || warn "${name}: fetch ${sha} не удался, проверяю локальную историю"
    if ! git -C "${dst}" checkout --detach "${sha}" >/dev/null 2>&1; then
        warn "${name}: SHA ${sha} отсутствует в shallow-клоне — переклонирую полностью"
        rm -rf "${dst}"
        git clone "${url}" "${dst}"
        git -C "${dst}" checkout --detach "${sha}"
    fi
    log "${name}: $(git -C "${dst}" log -1 --format='%h %ci %s')"
}

# 5.1 GGUF-лоадеры: UnetLoaderGGUF, CLIPLoaderGGUF (Apache-2.0).
#     pip-зависимость пакета — gguf (чистый python, ставится всегда).
_pin_node "https://github.com/city96/ComfyUI-GGUF" \
    "${_custom_nodes}/ComfyUI-GGUF" \
    "6ea2651e7df66d7585f6ffee804b20e92fb38b8a"
_pip "${COMFYUI_PYTHON}" gguf

# 5.2 Sol-H3 Exact Runtime / SOL Attention (GPL-3.0-or-later). Requirements
#     тянет тяжёлые CUDA-пакеты (triton/nvidia-cutlass-dsl/cuda-python/tvm-ffi);
#     их установка не должна валить сборку — фиксируем в логе.
_pin_node "https://github.com/xmarre/ComfyUI-Sol-H3" \
    "${_custom_nodes}/ComfyUI-Sol-H3" \
    "fe6a96183a84a1b91a16fa042ff1f3ad3e9dd62a"
if [[ -f "${_custom_nodes}/ComfyUI-Sol-H3/requirements.txt" ]]; then
    if ! _pip "${COMFYUI_PYTHON}" -r "${_custom_nodes}/ComfyUI-Sol-H3/requirements.txt"; then
        warn "Sol-H3: pip -r requirements.txt не установился целиком (тяжёлые CUDA-зависимости) — образ собирается, см. лог сборки"
    fi
fi

# 5.3 Продолжение (MIT, 0.6.3). deps по requirements.txt, если он есть.
_pin_node "https://github.com/ttulttul/ComfyUI-Minimax-H3-Continuation" \
    "${_custom_nodes}/ComfyUI-Minimax-H3-Continuation" \
    "e1768d5fdfc6f9519d2090dcf78458c2d9625f80"
if [[ -f "${_custom_nodes}/ComfyUI-Minimax-H3-Continuation/requirements.txt" ]]; then
    if ! _pip "${COMFYUI_PYTHON}" -r "${_custom_nodes}/ComfyUI-Minimax-H3-Continuation/requirements.txt"; then
        warn "continuation: pip -r requirements.txt не установился — образ собирается, см. лог сборки"
    fi
fi

# 5.4 CLIPCached (MIT): ставится в образ, в графе по умолчанию НЕ
#     используется. Любые ошибки установки не должны валить сборку.
_pin_node "https://github.com/Mu5hr00moO/ComfyUI-MiniMaxH3-CLIPCached" \
    "${_custom_nodes}/ComfyUI-MiniMaxH3-CLIPCached" \
    "507c1ecd7c92439134e097e0fac34381340ab8c9"
if [[ -f "${_custom_nodes}/ComfyUI-MiniMaxH3-CLIPCached/requirements.txt" ]]; then
    if ! _pip "${COMFYUI_PYTHON}" -r "${_custom_nodes}/ComfyUI-MiniMaxH3-CLIPCached/requirements.txt"; then
        warn "CLIPCached: pip -r requirements.txt не установился — образ собирается, см. лог сборки"
    fi
fi

log "custom_nodes в образе:"
ls -1 "${_custom_nodes}"
log "установка ComfyUI/публичных пакетов завершена"
