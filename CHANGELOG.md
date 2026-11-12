# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/). The job contract is
versioned separately as `h3-serverless/v1.1` (see
[`contract/worker-contract.md`](contract/worker-contract.md)).

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
