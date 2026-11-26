# Worker contract — h3-serverless/v1.1

This document is the source of truth for the job input and output of the worker in
this repository. The contract is versioned separately from the worker release: an
incompatible change is a new contract version (`v2`).

The worker executes a prepared ComfyUI API graph on a local ComfyUI instance and
returns the generated video plus execution-fact telemetry; an empty video is never
a successful result. JSON Schemas and examples:
[`input.schema.json`](input.schema.json), [`output.schema.json`](output.schema.json),
[`examples/`](examples/).

## 1. Job input

`POST {RUNPOD_SERVERLESS_API_BASE}/v2/{endpoint_id}/run`

```json
{
  "input": {
    "prompt": {"1": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {"prompt": "a cat walking"}}},
    "parameter": {"orientation": "9:16", "visible_frames": 141, "sound": "native"},
    "acceleration": {"gpu_class": "RTX PRO 6000", "steps": 8, "use_turbo": true}
  },
  "policy": {"executionTimeout": 3600000, "ttl": 3600000}
}
```

| Field | Meaning |
|---|---|
| `prompt` | Prepared ComfyUI API graph (`node id -> {class_type, inputs}`), submitted unchanged; the worker does not build or edit it, and ComfyUI rejects invalid graphs (`comfy_submit_failed`). Required unless `action` is `health`. |
| `parameter` / `acceleration` | Snapshots of generation parameters (`orientation`, `visible_frames`, `sound`, `mode`, `pip`, ...) and acceleration settings (`gpu_class`, `steps`, `use_turbo`, `lora_name`, `sampler`, `scheduler`, ...). Recorded for tracing; never used to fill telemetry. |
| `action` | `"health"` runs the health check without a prompt, a model load or a graph. Any other value fails with `input_invalid`. |
| `assets` | Optional input files delivered as presigned GET URLs; see below. |
| `policy` | Optional RunPod policy (`executionTimeout`, `ttl`). The worker does not read it; RunPod applies it before the handler runs. |

`input.assets` is a list of at most 32 entries. Each entry has a required `name`
(basename without path segments, `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`) and `url`
(HTTP(S) presigned GET URL), plus optional `sha256` (64 lowercase hex), `kind`
(`image`/`video`/`audio`) and `size` in bytes; no other fields are accepted. The
worker validates the list before waiting for ComfyUI, downloads each file into
the ComfyUI input directory under `name`, verifies `sha256` and the declared
size, and atomically replaces the target file; the graph references those names
in `LoadImage` / `LoadVideo` / `LoadAudio`. Limits: `ASSETS_MAX_MB` per file and
`ASSETS_MAX_TOTAL_MB` in total (defaults 512 MB / 2048 MB); declared sizes are
checked before download, actual sizes while streaming.

## 2. Job output

The handler returns one object that RunPod places in `output` of the status
response.

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

- Exactly one of `video` (base64) or `video_url` (presigned URL) is present.
  Results up to `OUTPUT_INLINE_MAX_MB` and within `OUTPUT_INLINE_BUDGET_MB` of
  encoded payload are returned inline; larger files go to S3-compatible storage
  as `video_url` with lifetime `OUTPUT_URL_TTL_SECONDS`.
- `telemetry` is required; the minimum is defined by
  [`output.schema.json`](output.schema.json): `gpu_cc`, `gpu_class`, `sol_called`,
  `steps`, `orientation`, `visible_frames`, `audio`, `cache`, `seconds`.
  Additional v1.1 fields: `sol_backend`, `lora_name`, `lora_hash`, `lora_applied`,
  `mode`, `pip`, `continuation_nodes`, `window_length`, `overlap_frames`,
  `extension_frames`, `append_frames`, `latent_tail_broken`,
  `first_frame_connected`, `last_frame_connected`.
- Cold-start facts (for cost accounting): the first generation job of a worker
  returns `cold_start_ms` (process start → ComfyUI ready), `worker_started_at`,
  `job_received_at`, `handler_ready_at` and `first_job=true`. Later jobs omit
  `cold_start_ms`; the worker is warm. The value covers container/ComfyUI boot,
  not model loading done by the first prompt — billing reconciliation in the
  application covers the full billed worker time.
- Telemetry comes from execution facts: `gpu_cc`/`gpu_class` from `torch.cuda`;
  `seconds` measured from submit to history completion; `visible_frames`, `audio`
  and `orientation` from `ffprobe` on the produced mp4; `steps`, `sol_called`,
  `lora_name`, `mode`, `pip` and the continuation fields from the executed graph;
  `lora_hash` is the sha256 of the LoRA file on the volume; `cache` is a fact or
  `"miss"` when the optional caching package is not used. Telemetry reported by
  the graph execution itself is used as-is.
- Expected values from `parameter`/`acceleration` are never substituted for
  measured facts. If a fact cannot be measured the job fails with
  `telemetry_missing`, or `telemetry_ambiguous` when sampler steps in the
  executed graph disagree.
- A graph without audio nodes is valid. When the client asks for a silent result
  (`"sound": "off"` in `parameter`), the graph omits `VAEDecodeAudio` and the
  `audio` input of `CreateVideo`, so the produced mp4 has no audio track and
  telemetry reports the measured `"audio": false`. The worker neither builds nor
  edits the graph and does not interpret `parameter.sound`: `audio` always
  describes the file that was actually produced.

## 3. Health

```json
{"status": "ok", "comfyui": "ready", "volume_mounted": true}
```

- `status`: `ok` (ComfyUI ready and the volume mounted), `starting` (ComfyUI
  booting, volume mounted; normal transient state) or `degraded` (volume missing
  and/or ComfyUI unavailable).
- `comfyui`: `ready`, `booting` or `unavailable` — result of a short
  `GET /system_stats` probe.
- `volume_mounted`: whether `VOLUME_ROOT` exists. Health never loads a model or a
  graph, so it is safe as a smoke test and for diagnostics.

## 4. Errors and cancellation

Any handler error fails the RunPod job (`FAILED`); error codes are stable
lowercase identifiers:

| Group | Codes |
|---|---|
| Input/config | `input_invalid`, `input_prompt_missing`, `config_invalid` |
| Assets | `asset_invalid`, `asset_download_failed`, `asset_hash_mismatch`, `asset_too_large` |
| ComfyUI | `comfy_boot_timeout`, `comfy_submit_failed`, `comfy_execution_failed`, `comfy_timeout`, `comfy_output_missing` |
| Telemetry | `telemetry_missing`, `telemetry_ambiguous`, `lora_hash_unavailable`, `gpu_unavailable`, `media_probe_failed` |
| Output | `output_empty`, `output_storage_missing`, `output_storage_failed` |

- Cancellation (`POST /v2/{endpoint_id}/cancel/{run_id}`) interrupts the running
  graph in ComfyUI before the job terminates. `JOB_TIMEOUT_SECONDS` (default
  10800) bounds the graph run inside the handler; the endpoint `timeout` and
  `policy.executionTimeout` must be greater than or equal to it, otherwise RunPod
  aborts the job before the worker can return an explicit error.

## 5. Environment

The worker reads its settings from endpoint environment variables. Invalid values
fail the job with `config_invalid` and the variable name.

| Variable | Default | Purpose |
|---|---|---|
| `COMFY_URL` | `http://127.0.0.1:8188` | Local ComfyUI address; loopback only. |
| `COMFYUI_DIR` / `COMFYUI_INPUT_DIR` | `/workspace/runpod-slim/ComfyUI` / `${COMFYUI_DIR}/input` | ComfyUI root and directory for `input.assets`. |
| `COMFY_BOOT_TIMEOUT_SECONDS` | `1200` | Wait for ComfyUI to start. |
| `JOB_TIMEOUT_SECONDS` | `10800` | Maximum graph run time (alias `COMFY_JOB_TIMEOUT_SECONDS`). |
| `COMFY_POLL_INITIAL_SECONDS` / `COMFY_POLL_MAX_SECONDS` / `COMFY_REQUEST_TIMEOUT_SECONDS` | `0.5` / `5.0` / `60` | `/history` polling backoff and per-request HTTP timeout. |
| `VOLUME_ROOT` / `MODELS_ROOT` | `/runpod-volume` / `${VOLUME_ROOT}/models` | Volume mount point and root of model weights (used for LoRA hashing). |
| `OUTPUT_INLINE_MAX_MB` / `OUTPUT_INLINE_BUDGET_MB` | `10` / `15` | Inline base64 file-size limit and encoded-payload budget. |
| `OUTPUT_URL_TTL_SECONDS` | `86400` | Lifetime of the presigned `video_url`. |
| `ASSETS_MAX_MB` / `ASSETS_MAX_TOTAL_MB` / `ASSETS_TIMEOUT_SECONDS` | `512` / `2048` / `120` | Per-file limit, total limit and download timeout for `input.assets`. |
| `FFPROBE_BIN` / `MEDIA_PROBE_TIMEOUT_SECONDS` | `ffprobe` / `30` | Media probe binary and timeout. |
| `HEALTH_TIMEOUT_SECONDS` | `10` | `/system_stats` timeout for the health action. |
| `LINK_VOLUME_NODES` | `false` | Link `custom_nodes` from the volume at boot. |
| `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET`, `S3_REGION`, `S3_SECURE` | — / `us-east-1` / auto | S3-compatible storage for large results; bucket alias `OUTPUT_BUCKET`. |
| `H3_GPU_CC` / `H3_GPU_CLASS` | — | Optional GPU fact override for environments without torch; set only as a pair. |

`S3_ACCESS_KEY` and `S3_SECRET_KEY` are secrets: set them only in the endpoint
environment, never in the image, the repository or logs. The image sets its own
boot variables; endpoint operators normally need to change only the variables in
this table.

Container boot variables are read by `docker/entrypoint.sh` and the volume
scripts, not by the handler: `VOLUME_AUTOPOPULATE` (default `true`) and
`HF_ENDPOINT`/`HF_TOKEN` control the model download, `COMFYUI_PYTHON`/`H3_PYTHON`
select interpreters, and `COMFY_START_CMD`/`COMFY_KEEP_EXTRA` override how
ComfyUI is started and configured.

## 6. Version

Current contract version: **`h3-serverless/v1.1`**. Input and output are additive
within a minor version. An incompatible change requires a new contract version
and a matching update of the schemas, examples and this document.
