# MiniMax H3 Sol-Turbo — RunPod Serverless Worker

RunPod serverless worker for **MiniMax H3** video generation with synchronized
audio. It runs ComfyUI v0.44.0 on a 96 GB Blackwell GPU, executes a prepared
ComfyUI API graph with the Sol-H3 exact runtime, 8-step turbo LoRAs and
multi-window continuation, and returns the generated MP4 plus telemetry measured
from the actual execution. It does not build or modify the graph, does not ship
model weights and keeps ComfyUI on loopback only. Contract:
**`h3-serverless/v1.1`** — [`contract/worker-contract.md`](contract/worker-contract.md);
changes are listed in [`CHANGELOG.md`](CHANGELOG.md).

## Deploy

1. **Storage first.** Create a network volume of at least **100 GB**
   (150 GB recommended) in a datacenter that offers 96 GB Blackwell GPUs; it
   holds about 62 GB of models plus video and temporary files.
2. **Deploy the worker.** Publish this repository as a GitHub release to
   [RunPod Hub](https://console.runpod.io/hub) (metadata:
   [`.runpod/hub.json`](.runpod/hub.json) — `BLACKWELL_96`, CUDA 13.0–13.2,
   30 GB container disk), or build the image and create a serverless endpoint
   yourself.
3. **Attach the volume** at `/runpod-volume`, use endpoint type `QUEUE` and set
   **Max workers = 1**: a cold worker loads tens of gigabytes of weights, so one
   warm worker with queued jobs beats several cold ones.
4. **Wait for the first boot.** With `VOLUME_AUTOPOPULATE=true` (default) the
   container downloads missing files from
   [`models-manifest.txt`](models-manifest.txt); later boots reuse the volume.
5. **Smoke-test** with the health action (see [Health](#health)); RunPod Hub runs
   this test on build ([`.runpod/tests.json`](.runpod/tests.json)).
6. **Keep secrets in the endpoint environment only** (`S3_*` for large results,
   optional `HF_TOKEN` for gated Hugging Face mirrors).

### Requirements

| Item | Requirement |
|---|---|
| GPU | 96 GB Blackwell (`BLACKWELL_96`, e.g. RTX PRO 6000), compute capability 12.0 |
| CUDA, endpoint | CUDA 13.0+ host driver (13.0–13.2; CUDA is backward compatible within 13.x, so newer drivers are allowed); `QUEUE` type, max workers 1, timeout ≥ `JOB_TIMEOUT_SECONDS` |
| Network volume | at least 100 GB at `/runpod-volume` (150 GB recommended) |
| Registry | The built image must be available to RunPod (Hub build or your registry) |
| 32 GB GPUs | Not supported out of the box (RTX 5090 needs a smaller quant and text-encoder offload) |

## Build the image

The root [`Dockerfile`](Dockerfile) builds on the official ComfyUI v0.44.0
CUDA 13.0 image pinned by digest and adds the worker package, pinned nodes,
`handler.py` and the volume scripts.

```bash
docker build -t <registry>/minimax-h3-sol-turbo-serverless:1.0.0 .
docker push <registry>/minimax-h3-sol-turbo-serverless:1.0.0
```

Structural build without ComfyUI or a GPU (checks that the Dockerfile, package
and scripts are wired; not meant to run):

```bash
docker build --build-arg BASE_IMAGE=python:3.12-slim \
  --build-arg SKIP_COMFY_PACKAGES=1 -t h3-serverless:stub .
docker run --rm --entrypoint python h3-serverless:stub -c "import h3_worker"
```

Optional: `H3_WORKER_VERSION` (`1.0.0`), `COMFYUI_VERSION` (`v0.44.0`). Pin by
digest: `docker inspect --format='{{index .RepoDigests 0}}' <image>`.

## Job input

Submit to the endpoint's `/run` API; minimal shape (abbreviated graph):

```json
{
  "input": {
    "prompt": {"1": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {"prompt": "a cat walking"}}},
    "parameter": {"orientation": "9:16", "visible_frames": 141, "sound": "native"},
    "acceleration": {"gpu_class": "RTX PRO 6000", "steps": 8, "use_turbo": true}
  }
}
```

| Field | Description |
|---|---|
| `prompt` | Complete ComfyUI API graph (node id to `{class_type, inputs}`), submitted unchanged. Required unless `action` is `health`. |
| `parameter` / `acceleration` | Generation and acceleration snapshots (`orientation`, `visible_frames`, `steps`, `use_turbo`, ...): tracing and telemetry input only. |
| `assets` | Optional input files as presigned URLs `{name, url, sha256?, kind?, size?}`, downloaded into ComfyUI's input directory under `name`; the graph references those names. |
| `action` | `"health"` runs a model-free health check; no prompt needed. |
| `policy` | Optional RunPod policy; the worker does not read it. |

More examples: [`request.json`](contract/examples/request.json),
[`request-assets.json`](contract/examples/request-assets.json).

## Job output

```json
{
  "video": "<base64 mp4>",
  "telemetry": {
    "gpu_cc": 12.0, "gpu_class": "RTX PRO 6000", "sol_called": true,
    "steps": 8, "orientation": "9:16", "visible_frames": 141,
    "audio": true, "cache": "miss", "seconds": 143.2
  }
}
```

- Exactly one of `video` (base64, small results) or `video_url` (presigned URL,
  large results in S3-compatible storage) is returned.
- `telemetry` is always measured, never copied from `parameter`/`acceleration`:
  `gpu_cc`/`gpu_class` from `torch.cuda`; `seconds` from submit to completion;
  `visible_frames`, `audio`, `orientation` from `ffprobe` on the MP4; `steps`,
  `sol_called`, `lora_name`, `mode`, `pip`, `continuation_*` from the graph;
  `lora_hash` is the LoRA sha256 and `cache` a fact or `miss`.
- Exact minimum: [`contract/output.schema.json`](contract/output.schema.json);
  examples: [`response.json`](contract/examples/response.json),
  [`response-video-url.json`](contract/examples/response-video-url.json).

## Health

Request (`POST /run`): `{"input": {"action": "health"}}`

```json
{"status": "ok", "comfyui": "ready", "volume_mounted": true}
```

`status` is `ok`, `starting` (ComfyUI is still booting — normal after a cold
start) or `degraded` (volume missing and/or ComfyUI unavailable). Health never
loads a model or runs a graph.

## Errors and cancellation

A failed job ends as `FAILED`; the error text carries a stable code. An empty
video is never a success.

| Group | Codes |
|---|---|
| Input/config | `input_invalid`, `input_prompt_missing`, `config_invalid` |
| Assets | `asset_invalid`, `asset_download_failed`, `asset_hash_mismatch`, `asset_too_large` |
| ComfyUI | `comfy_boot_timeout`, `comfy_submit_failed`, `comfy_execution_failed`, `comfy_timeout`, `comfy_output_missing` |
| Telemetry | `telemetry_missing`, `telemetry_ambiguous`, `lora_hash_unavailable`, `gpu_unavailable`, `media_probe_failed` |
| Output | `output_empty`, `output_storage_missing`, `output_storage_failed` |

Cancelling a job interrupts the running graph in ComfyUI, so the GPU is not kept
busy. Keep the endpoint timeout at or above `JOB_TIMEOUT_SECONDS` (default 10800).
Full contract: [`contract/worker-contract.md`](contract/worker-contract.md).

## Models on the network volume

Models are not shipped in the image. On first boot `populate-volume.sh`
downloads missing files from [`models-manifest.txt`](models-manifest.txt) into
`/runpod-volume/models/<subdir>/<file>`, the layout ComfyUI sees through
`extra_model_paths.yaml`.

| Volume path | Source |
|---|---|
| `diffusion_models/MiniMax-H3-FL2VA-Q4_K_M.gguf` | [realrebelai/MiniMax-H3_GGUFs](https://huggingface.co/realrebelai/MiniMax-H3_GGUFs) (pinned revision) |
| `diffusion_models/MiniMax-H3-REF2VA-Q4_K_M.gguf` | same repository |
| `text_encoders/qwen3vl-32B-MiniMax-H3-Q4_K_M.gguf` | same repository |
| `loras/minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors`, `loras/minimax_h3_ref2v_turbo_8step_v1.0_768p_comfyui_resized_avg_rank_20_bf16.safetensors` | [drbaph/MiniMax-H3-Turbo-Lora-ComfyUI](https://huggingface.co/drbaph/MiniMax-H3-Turbo-Lora-ComfyUI) (pinned revision) |
| `vae/minimax_h3_video_vae_fp16.safetensors`, `vae/minimax_h3_audio_vae_fp32.safetensors` | [Comfy-Org/MiniMax-H3](https://huggingface.co/Comfy-Org/MiniMax-H3) (pinned revision) |

The full set is about 62 GB. Boot variables: `VOLUME_AUTOPOPULATE` (default
`true`), `HF_TOKEN` (optional, gated repositories only) and `HF_ENDPOINT`
(optional mirror); the download target is `$VOLUME_ROOT/models`. For other
checkpoints, adapt `models-manifest.txt` and keep graph filenames in sync.

## Custom nodes in the image

The image is built on `runpod/comfyui:2.2.0-comfyuiv0.44.0-cuda13.0`, pinned by
digest
(`sha256:adccffa4dc2151b48dd7110b7b03a5f9450b059bc8524bc493b93e5155aea3e2`), with
ComfyUI v0.44.0 and its native H3 nodes plus:

| Package | Revision | License | Provides |
|---|---|---|---|
| ComfyUI-GGUF | `6ea2651e` | Apache-2.0 | `UnetLoaderGGUF`, `CLIPLoaderGGUF` |
| ComfyUI-Sol-H3 | `fe6a9618` | GPL-3.0-or-later | `SolH3Exact`, `SolH3Experimental` |
| ComfyUI-Minimax-H3-Continuation | `e1768d5f` | MIT | guided-continuation nodes |
| ComfyUI-MiniMaxH3-CLIPCached | `507c1ecd` | MIT | optional conditioning cache; unused by default, so `cache` is `miss` |

## Endpoint environment

| Variable | Default | Purpose |
|---|---|---|
| `VOLUME_AUTOPOPULATE` | `true` | Download missing models to the volume at boot. |
| `HF_TOKEN` | — | Token for gated Hugging Face repositories (optional). |
| `JOB_TIMEOUT_SECONDS` | `10800` | Maximum graph run time inside the worker. |
| `COMFY_BOOT_TIMEOUT_SECONDS` | `1200` | How long to wait for the local ComfyUI to start. |
| `LINK_VOLUME_NODES` | `false` | Link `custom_nodes` from the volume at boot (the image already bakes the nodes). |
| `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET` | — | S3-compatible storage for large `video_url` results. |

The complete list — inline output thresholds, asset limits, polling, GPU
overrides — is in the contract's environment table.

## Repository layout

Root: `Dockerfile`, `handler.py`, `models-manifest.txt`, `populate-volume.sh`,
`pyproject.toml`. `.runpod/` holds the Hub listing; `contract/` the v1.1 contract
and examples; `src/h3_worker/` the worker; `docker/` and `scripts/` image helpers.

## Licenses

Worker code is MIT (see [`LICENSE`](LICENSE)). The image includes ComfyUI
(GPL-3.0), ComfyUI-Sol-H3 (GPL-3.0-or-later), ComfyUI-GGUF (Apache-2.0),
ComfyUI-Minimax-H3-Continuation and ComfyUI-MiniMaxH3-CLIPCached (MIT), plus the
NVIDIA CUDA and PyTorch components of the base image under their own terms.
Publishing the image means distributing these components: keep their notices and
make sources available where required.

Model weights and LoRAs are not part of this repository or the image; they are
downloaded by the operator to a network volume and remain governed by the
licenses of their Hugging Face repositories. Review them before use.

## Limitations

- The worker executes the graph you send: it does not build, repair or validate
  it, and the graph must use node types available in the image.
- Weights are not bundled; a volume without the expected files fails the job
  visibly instead of silently falling back.
- 32 GB Blackwell GPUs need a smaller quant and are not supported out of the box.
- CLIPCached is installed but not used by the default graph (`cache: "miss"`);
  picture-in-picture, voice and lip-sync are not implemented (`pip: false`).
- Cold start is heavy (about 62 GB on first boot, then model loading per worker):
  prefer one warm worker with a queue and benchmark timeouts on your endpoint.
