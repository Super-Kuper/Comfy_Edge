# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/). The job contract is
versioned separately as `h3-serverless/v1.1` (see
[`contract/worker-contract.md`](contract/worker-contract.md)).

## [1.1.1] - 2026-11-26

### Fixed

- Hub image build no longer depends on the deleted upstream
  `Mu5hr00moO/ComfyUI-MiniMaxH3-CLIPCached` repository. The build failed at
  step 4/14 because `git clone` of that repository asked for credentials
  (GitHub returned 404 after the repository was removed). The same pinned
  commit `507c1ecd` is now installed from public forks; the SHA pin keeps the
  installed code identical. A second mirror is tried, and if neither serves
  the pin the image builds without this optional node (the default graph does
  not use it), with a warning in the build log instead of a failed build.
- `_pin_node` now returns a non-zero status when clone/checkout fails, so an
  optional node can be handled with `if _pin_node ...`; critical nodes still
  abort the build via `set -e`.

## [1.1.0] - 2026-11-26

### Added

- Cold-start facts for cost accounting: the first generation job of a worker
  reports `cold_start_ms` (process start → ComfyUI ready), `worker_started_at`,
  `job_received_at`, `handler_ready_at` and `first_job`. The application uses
  them for per-batch cost; full billed cold start is reconciled from RunPod
  billing.

### Changed

- Contract clarification (no runtime change): a graph without audio nodes is
  valid, and the measured `telemetry.audio` is `false` for a silent output. The
  worker neither builds nor edits the graph and does not interpret
  `parameter.sound`; `audio` is always the fact of the produced file (see
  [`contract/worker-contract.md`](contract/worker-contract.md) §2).

## [1.0.3] - 2026-11-12

### Fixed

- Hub build/validation pod deployment on Blackwell GPUs (the CUDA ceiling moved
  again): `allowedCudaVersions` in [`.runpod/hub.json`](.runpod/hub.json) and
  [`.runpod/tests.json`](.runpod/tests.json) now lists CUDA `13.0`–`13.4`.
  The field is an exact-match list, not a minimum, and RunPod hosts for
  RTX PRO 6000 Blackwell now advertise 13.3/13.4, so with the previous
  `13.0`–`13.2` list the Hub validator saw no eligible machines and failed with
  "There were no instances available" for `BLACKWELL_96`. The image is built on
  CUDA 13.0 and newer 13.x drivers are backward compatible, so those hosts run
  it unchanged.
- The same list is used for endpoint deployments, so workers keep being placed
  on the whole `BLACKWELL_96` fleet instead of only hosts on older CUDA
  versions.

## [1.0.2] - 2026-11-12

### Fixed

- Contract request examples now use a canvas aligned to the H3 latent grid:
  [`contract/examples/request.json`](contract/examples/request.json) and
  [`contract/examples/request-assets.json`](contract/examples/request-assets.json)
  use `768x1344` for 9:16 instead of `768x1360`. At `768x1360` the video latent
  is 85x48, so `MiniMaxH3ImageToVideo` failed in `patchify_video` on the odd
  side; the node requires dimensions divisible by 32 (9:16 — `768x1344`,
  16:9 — `1344x768`).

## [1.0.1] - 2026-11-12

### Fixed

- Hub build/validation pod deployment on Blackwell GPUs: `allowedCudaVersions`
  in [`.runpod/hub.json`](.runpod/hub.json) and
  [`.runpod/tests.json`](.runpod/tests.json) now lists CUDA `13.1` and `13.2`
  next to `13.0`. The image is built on CUDA 13.0, and CUDA is backward
  compatible within a major version, so hosts advertising newer 13.x drivers run
  it unchanged. With only `13.0` listed, most RTX PRO 6000 capacity was filtered
  out and the Hub validator failed with "There were no instances available"
  for `BLACKWELL_96` even though GPUs were free.
- Hub build validation now targets the most available 96 GB Blackwell variant:
  [`.runpod/tests.json`](.runpod/tests.json) uses
  `NVIDIA RTX PRO 6000 Blackwell Server Edition` (the health check is
  GPU-agnostic inside `BLACKWELL_96`), so the validator pod does not depend on
  the scarcer Workstation/Max-Q stock.

## [1.0.0] - 2026-11-12

First public release: a RunPod serverless worker for MiniMax H3 video + audio
generation on ComfyUI v0.44.0 (CUDA 13.0).

### Added

- RunPod Hub packaging: root `Dockerfile`, root `handler.py`,
  `.runpod/hub.json`, `.runpod/tests.json`, `models-manifest.txt` and
  `populate-volume.sh`.
- Contract `h3-serverless/v1.1`: prepared ComfyUI API graph in, exactly one of
  `video` (inline base64) or `video_url` (presigned S3 URL) plus execution-fact
  telemetry out; JSON Schemas and request/response examples under
  [`contract/`](contract/).
- Worker implementation (`src/h3_worker/`): ComfyUI client, input-asset
  delivery (presigned URLs, sha256 and size checks), `ffprobe` media probing,
  telemetry assembly from the executed graph, S3/MinIO delivery, health action,
  cancellation via ComfyUI `/interrupt` and visible error codes.
- Image built on the official `runpod/comfyui:2.2.0-comfyuiv0.44.0-cuda13.0`
  base pinned by digest, with ComfyUI v0.44.0 plus pinned public custom nodes:
  ComfyUI-GGUF, ComfyUI-Sol-H3, ComfyUI-Minimax-H3-Continuation and
  ComfyUI-MiniMaxH3-CLIPCached.
- Network-volume model provisioning: `populate-volume.sh` downloads the pinned
  GGUF DiTs, Qwen3-VL text encoder, turbo LoRAs and VAEs listed in
  `models-manifest.txt` (about 62 GB) on first boot (`VOLUME_AUTOPOPULATE`).
- Optional S3-compatible storage for large results: `video_url` with
  `OUTPUT_URL_TTL_SECONDS` lifetime instead of inline base64.

### Notes

- Model weights and LoRAs are not shipped in the image or repository: they are
  downloaded to the attached network volume and remain subject to the licenses
  of their Hugging Face repositories.
- The image contains GPL-3.0 (ComfyUI) and GPL-3.0-or-later (Sol-H3)
  components; review the license notes in the README before redistributing the
  image.
