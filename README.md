# MuseAI-API

> Unofficial Python client for Muse.ai, reconstructed from an authorized browser capture and the Muse/Hatch web client protocol.

MuseAI-API provides a Python interface for authenticating with Muse, connecting to the Hatch backend over Noise Protocol, submitting video-generation prompts, tracking generation, and downloading generated media.

## Features

- Native Muse OTP login with reusable local session
- Browser-compatible authentication flow using Chrome impersonation
- Hatch VM wake and fresh admission token bootstrap
- WebSocket transport over `Noise_XX_25519_AESGCM_SHA256`
- Protobuf framing, chunk reassembly, and multiplexed RPC streams
- Text-to-video generation
- Image-to-video generation with optional reference images
- Chat/session discovery and generation tracking through `chat.history`
- Generated video detection and MP4 download
- Muse filesystem helpers for read/write/upload operations
- 512 KiB chunked file upload support

## Requirements

- Python 3.11+
- A Muse.ai account you control
- Windows, Linux, or macOS

## Installation

Clone the repository:

```bash
git clone https://github.com/minhquan130599/MuseAI-API.git
cd MuseAI-API
```

Create a virtual environment.

### Windows

```bat
py -3.12 -m venv .venv
.venv\Scripts\activate
python -m pip install -U -e ".[dev]"
```

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U -e ".[dev]"
```

## MCP Server

The `mcp-server` branch exposes Muse as a Model Context Protocol server so Claude, Codex, VS Code agents, and other MCP-compatible clients can communicate with Muse directly.

Install/update the project:

```bat
python -m pip install -U -e ".[dev]"
```

Authenticate Muse once before starting MCP:

```bat
muse-ai login --email YOUR_MUSE_EMAIL
```

### Start MCP over stdio

```bat
muse-mcp --transport stdio
```

or:

```bat
run_mcp.bat
```

The stdio transport is recommended for local agent integrations.

### MCP tools

| Tool | Purpose |
| --- | --- |
| `muse_auth_status` | Check whether the saved Muse session is valid |
| `muse_ping` | Verify Muse → Hatch → Noise RPC connectivity |
| `muse_model` | Read the active Muse model |
| `muse_send_text` | Send text to Muse and receive the assistant response |
| `muse_history` | Read chat history, optionally by session |
| `muse_generate_image` | Generate image(s) from text and optional ChatGPT/local reference images |
| `muse_generate_video` | Generate video from text; also supports optional file/local references |
| `muse_generate_video_from_images` | Preferred video tool when ChatGPT images are attached; `images` is required |
| `muse_job_status` | Poll an asynchronous image/video generation job |
| `muse_list_jobs` | List image/video jobs created by the MCP process |
| `muse_cancel_job` | Cancel a running asynchronous image/video job |

### Text-in / text-out mode

The primary agent-to-Muse tool is `muse_send_text`:

```json
{
  "prompt": "Explain this idea in three concise bullet points.",
  "timeout_seconds": 120
}
```

Example result:

```json
{
  "ok": true,
  "session_id": "...",
  "text": "..."
}
```

To continue the same Muse conversation, pass the returned `session_id` into the next call:

```json
{
  "prompt": "Now rewrite point 2 with more detail.",
  "session_id": "SESSION_ID_FROM_PREVIOUS_RESULT"
}
```

By default the MCP response does not include raw Muse stream/history payloads. Set `include_raw=true` only when debugging protocol behavior.

### Forward ChatGPT uploads to Muse

Both `muse_generate_video` and `muse_generate_image` expose an `images`
parameter as a native ChatGPT file input using:

```json
{
  "_meta": {
    "openai/fileParams": ["images"]
  }
}
```

At runtime ChatGPT can pass uploaded/reference images as:

```json
{
  "prompt": "Create a 10-second vertical 9:16 product video using these references.",
  "images": [
    {
      "download_url": "https://...",
      "file_id": "file_...",
      "mime_type": "image/jpeg",
      "file_name": "reference-01.jpg"
    }
  ]
}
```

The MCP server downloads those temporary URLs into:

```text
.muse-mcp/uploads/<request-id>/
```

then passes the local files to Muse as normal image attachments. Temporary
imported files are deleted after the blocking generation finishes, or after the
background job finishes/cancels.

The old `image_paths` input remains available for local agents that already
have filesystem access. When a ChatGPT request explicitly refers to attached
images, prefer `muse_generate_video_from_images`; its `images` field is required,
which prevents the model from silently falling back to text-only generation.

When `--public-base-url` is configured, the MCP bridge forces generated media
downloads even if a caller passes `download=false`, because public `/media/...`
links can only be created from locally downloaded output files. Muse/Hatch
`metaaivm.com/media/...` URLs are scrubbed from text/history responses so remote
clients do not surface session-bound dead links.

After changing tool metadata, refresh/reconnect the ChatGPT plugin connection
and start a new chat so ChatGPT loads the updated tool schema.

### Image mode

Muse exposes generated images as presentation kind `image` with `data.images[]` media records. The MCP bridge waits for those image presentations and downloads the media through the same Hatch media path used by the web client.

Blocking mode:

```json
{
  "prompt": "Create a photorealistic square image of a modern coffee shop at night, warm lighting, no text.",
  "wait": true,
  "timeout_seconds": 300,
  "min_images": 1
}
```

Asynchronous mode:

```json
{
  "prompt": "Create a photorealistic vertical poster-style image of Hanoi at night, no text.",
  "wait": false,
  "timeout_seconds": 300
}
```

The async call returns a `job_id`. Poll the same generic `muse_job_status` tool until the job is `completed` or `failed`.

Downloaded image results expose safe presentation metadata plus public tunnel `url` / `download_url` fields when `--public-base-url` is configured. Muse/Hatch internal media URLs are not returned to remote MCP clients.

CLI smoke test:

```bat
muse-ai generate-image ^
  --prompt "Create a photorealistic square image of a futuristic Hanoi street at night, neon reflections, no text." ^
  --output outputs ^
  --timeout 300
```

### Video mode

Blocking mode:

```json
{
  "prompt": "Create a realistic 10-second vertical 9:16 cinematic coffee shop video.",
  "wait": true,
  "timeout_seconds": 600
}
```

Asynchronous mode:

```json
{
  "prompt": "Create a realistic 10-second vertical 9:16 cinematic coffee shop video.",
  "wait": false,
  "timeout_seconds": 600
}
```

The asynchronous call immediately returns a `job_id`. Poll it with:

```json
{
  "job_id": "JOB_ID"
}
```

using `muse_job_status`.

Generated MCP video files default to:

```text
.muse-mcp/
└── outputs/
    └── <job_id>/
        └── *.mp4
```

### Claude / generic MCP client config

See `examples/mcp-config.json`.

Windows example:

```json
{
  "mcpServers": {
    "muse-ai": {
      "command": "E:\\MCP\\codexpro-main\\Extensions\\MuseAI-API\\.venv\\Scripts\\python.exe",
      "args": [
        "-m",
        "muse_ai.mcp_server",
        "--transport",
        "stdio",
        "--state-dir",
        "E:\\MCP\\codexpro-main\\Extensions\\MuseAI-API\\.muse-state"
      ]
    }
  }
}
```

### Codex config

See `examples/codex-config.toml`.

```toml
[mcp_servers.muse-ai]
command = "E:\\MCP\\codexpro-main\\Extensions\\MuseAI-API\\.venv\\Scripts\\python.exe"
args = [
  "-m",
  "muse_ai.mcp_server",
  "--transport",
  "stdio",
  "--state-dir",
  "E:\\MCP\\codexpro-main\\Extensions\\MuseAI-API\\.muse-state",
]
```

### Streamable HTTP mode

For a long-running local MCP service:

```bat
muse-mcp --transport streamable-http --host 127.0.0.1 --port 8765
```

Endpoint:

```text
http://127.0.0.1:8765/mcp
```

When exposing the server through a tunnel, pass its public origin with
`--public-base-url`. The public hostname is automatically added to the
MCP DNS-rebinding allowlist, and generated image/video files receive public
view/download URLs.

Example with Cloudflare Quick Tunnel:

```bat
muse-mcp ^
  --transport streamable-http ^
  --host 127.0.0.1 ^
  --port 8765 ^
  --public-base-url https://example.trycloudflare.com
```

Then expose the local server:

```bat
cloudflared tunnel --url http://127.0.0.1:8765
```

The MCP endpoint is:

```text
https://example.trycloudflare.com/mcp
```

Generated media responses include entries similar to:

```json
{
  "files": [
    {
      "filename": "video.mp4",
      "public_url": "https://example.trycloudflare.com/media/<opaque-id>",
      "download_url": "https://example.trycloudflare.com/media/<opaque-id>?download=1"
    }
  ]
}
```

The media route uses an opaque per-process identifier rather than exposing the
local filesystem path. Quick Tunnel hostnames change when the tunnel is
restarted, so restart `muse-mcp` with the new `--public-base-url` when that
happens.

The server binds to loopback by default. Do not expose the MCP HTTP endpoint
publicly without adding authentication, network controls, and access
restrictions because MCP tools can use your authenticated Muse account and
optional local image paths.

## Web UI

The `web-ui` branch includes a local FastAPI server and browser interface.

Install/update dependencies:

```bat
python -m pip install -U -e ".[dev]"
```

Start the web app:

```bat
muse-web --host 127.0.0.1 --port 8787
```

or on Windows:

```bat
run_web.bat
```

Open:

```text
http://127.0.0.1:8787
```

The web UI supports **a pool of multiple Muse accounts** with separate login/session state, OTP management, normal chat with account selection, and batch video generation across distinct ready accounts. Click the top-right account status to manage the account list. Paste **up to 10 email addresses** separated by commas/newlines and send OTP requests together; enter each OTP by selecting its account in the popup. You can verify, enable/disable, and remove accounts. A previous single-account session in `.muse-state/` is imported as a separate legacy entry on first launch if no account pool exists.

Each account has its own cookie/device session under `.muse-web/accounts/<id>/`. The accounts index stores only metadata, not authentication tokens. The account list shows which sessions are ready and which are currently running tasks.

In **Chat thường**, select an account before sending messages. The conversation picker loads account-scoped threads from the SQLite database `.muse-web/chat-history.sqlite3`, while keeping a browser backup. On page load, older browser-only conversations are imported into SQLite without deleting the local backup. Switching accounts restores the latest conversation for that account. The Muse `session_id` is never automatically reused with a different account. Legacy conversations without an `accountId` remain preserved in local storage but are not shown under another account. **+ Chat mới now creates a real secondary Muse thread:** the first message is sent to a fresh UUID `session_id`, and later messages reuse exactly that ID. Muse treats a missing `session_id` as the primary conversation, which is why older builds sent new chat messages into the main thread. Already-saved browser conversations containing the incorrect primary `session_id` cannot be retroactively separated; create a fresh chat after upgrading.

The **Tạo video** button uses the live account-pool availability, not a separate legacy single-login check; it becomes enabled when the pool has at least N ready/idle accounts and disabled only when the task count exceeds that capacity (or submission is underway). In **Tạo video**, set **Số task / số tài khoản** to N (1–20). A single submit creates N independent jobs with the same prompt/reference images; the scheduler randomly selects **N different, enabled, verified and currently idle accounts** from the pool and reserves each account until its task finishes or is cancelled. If fewer than N accounts are available, it returns HTTP 409 rather than silently reusing an account. Batch results include `batch_id`, `account_id`, `account_label` and per-job status/download URLs. The default web concurrency is 20; adjust with `MUSE_WEB_CONCURRENCY` if required. This does not bypass Muse's account quotas, subscriptions, or usage restrictions.

**Important:** The web app is designed for trusted local access. Do not expose this multi-account control panel to the public Internet without implementing administrator authentication and access controls. Local uploaded chat files are served only to this trusted local web interface, not protected by a separate account-authentication layer.

**Chat attachments:** In Chat thường, use **＋ Đính kèm tệp** to select up to 10 files together (12 MB per file, 40 MB total). Images (PNG/JPG/WEBP/GIF) are embedded in Muse's known image-item format. Documents (PDF/TXT/MD/CSV/JSON/DOCX/XLSX/PPTX) are uploaded via Muse filesystem and their workspace paths are supplied in the chat prompt. Because the captured HAR does not include Muse's native binary document-attachment schema, file access through these workspace references depends on the tools Muse grants to that session; it is **not** a claim of native file-item support. Selected filenames and server-held original files are retained in `.muse-web/chat-uploads/` and the SQLite history even when the browser is closed. Text-only chat requests still use JSON and existing clients remain compatible.

**Completion state:** The 2026-09-28 HAR supplied by the user contains telemetry POSTs to `/api/falco` and `/api/analytics`, but no `/chat/stream`, `/chat/history`, or Muse Hatch WebSocket frames. Thus it cannot prove the exact internal event that stops Muse's browser typing indicator. The API treats a recognized explicit assistant final marker as final, and labels the 8-second idle text fallback **provisional** while history polling continues; only an explicitly terminal video task can cause an early `muse_finished_without_video`. Unknown progress remains unknown instead of being misreported as failure.

Use **Chat thường** to send ordinary messages (Enter to send, Shift+Enter for a new line). Muse text replies appear after the response is considered complete or passes the fallback idle check. Generated image/video attachments and supported file references are checked asynchronously from Muse chat history, downloaded to `.muse-web/chat-media/`, and previewed/linked for download in the same chat bubble when ready. File monitoring can continue after an initial response such as "Xong"; only successfully downloaded local files produce browser links. The backend stores user and assistant turns in SQLite; the browser's localStorage remains only a backup. Choose **+ Chat mới** to start a separate Muse conversation. **↻ Đồng bộ lịch sử từ Muse** re-fetches the selected thread using its saved `session_id`; to recover a thread known only by ID, create a new conversation in the correct Muse account, enter the `session_id`, and click Sync. The sync merges messages without discarding local turns/attachments that Muse did not return. Muse history responses may have service-side limits or changing formats; an incomplete/unsupported response does not erase existing SQLite history. Longer text responses prefer an explicit completion marker; without one, an 8-second idle period is used as a fallback, followed by server-side reconciliation around 20 and 60 seconds after the reply. A quiet stream is not proof of completion.

REST API documentation is available at:

```text
http://127.0.0.1:8787/docs
```

Web job metadata and downloaded media are stored under `.muse-web/`, which is excluded from Git. Closing the browser tab does not stop a running job, but stopping the `muse-web` process does. Generated-video job cards use a compact display: prompts are collapsed to two lines, videos are hidden behind **Xem / tải video**, and verbose download diagnostics are collapsed. Expanding a video no longer resets playback when unrelated jobs refresh. Media `fs.raw` HTTP 404 means a referenced backing file is not available; the client retries once. If at least one MP4 was downloaded but another reference failed, the job is marked `completed_partial` and its valid MP4 remains playable.

> The web server binds to `127.0.0.1` by default. Do not expose it publicly unless you add your own access control and deployment security.

## Quick Start

### 1. Login

```bat
muse-ai login --email YOUR_MUSE_EMAIL
```

Enter the OTP when prompted.

The reusable session is stored locally under:

```text
.muse-state/
├── cookies.json
└── device.json
```

The `.muse-state/` directory is ignored by Git and must never be committed.

### 2. Verify the connection

```bat
muse-ai ping
muse-ai model
muse-ai history
```

A successful `ping` confirms the main connection path is working:

```text
Muse session
   ↓
Hatch bootstrap
   ↓
WebSocket
   ↓
Noise XX handshake
   ↓
Encrypted RPC
   ↓
/api/ping
```

## Text-to-Video

No reference image is required.

```bat
muse-ai generate ^
  --prompt "Create a realistic 10-second vertical 9:16 cinematic video of a modern coffee shop at night. Warm ambient lighting, people naturally walking in the background, slow camera push-in toward the counter, realistic human movements, cinematic depth of field, smooth motion, highly detailed, commercial video quality, no subtitles, no text overlay, no watermark." ^
  --output outputs ^
  --timeout 600
```

The request sent to Muse contains a text item similar to:

```json
{
  "items": [
    {
      "type": "text",
      "text": "Create a realistic 10-second vertical 9:16 cinematic video..."
    }
  ]
}
```

## Image-to-Video

Reference images are optional. Add one or more `--image` arguments when needed.

```bat
muse-ai generate ^
  --image "D:\images\01.jpg" ^
  --image "D:\images\02.jpg" ^
  --image "D:\images\03.jpg" ^
  --prompt "Create a 10-second vertical 9:16 product video using all reference images in sequence. Use smooth realistic transitions, preserve the main product, use natural hand movement and commercial-quality lighting." ^
  --output outputs ^
  --timeout 600
```

To wait for at least two generated videos:

```bat
muse-ai generate ^
  --prompt "Create two cinematic product video variations." ^
  --output outputs ^
  --min-videos 2 ^
  --timeout 600
```

## Output

Generated videos are downloaded to the directory passed through `--output`.

Default:

```text
outputs/
├── generated-video-1.mp4
└── generated-video-2.mp4
```

Generation happens asynchronously on Muse servers. The client:

1. reads the current chat history as a baseline;
2. sends the prompt through `POST /chat/stream`;
3. discovers the active/new session;
4. polls `/chat/history` for an **explicit** assistant completion marker and for any active video-generation task;
5. detects new video media and downloads the generated result;
6. reports `muse_finished_without_video` with the original Muse reply only when the new assistant text is final, a **new, identifiable video task** (not a task already completed before sending) explicitly reports terminal state (failed/cancelled/completed), and no media appears in two checks. If Muse provides no video-task status, the chat-complete flag is insufficient: polling continues until media or timeout.

**Important distinction:** a completed assistant message is not necessarily a completed asynchronous video render. The watcher ignores a completed chat message as a video failure while the history explicitly contains an active video task. If the Muse service omits job state or completion flags from its API, the client cannot know reliably from the website's visual typing indicator alone. The early error is therefore based on observable protocol state, not guaranteed to mirror the browser UI. When completion cannot be confirmed, monitoring continues until video or timeout.

In **Chat thường**, the media watcher likewise reports `muse_finished_without_video` and shows the complete Muse text when an explicitly finished matching assistant response has no new video and no active render task. If generation succeeds but a download source is temporarily unavailable, the client keeps the generation result and reports a download warning instead of treating the entire generation as failed.

## Python API

```python
import asyncio

from muse_ai import MuseAuth, MuseClient


async def main():
    auth = MuseAuth()
    client = MuseClient(auth)

    await client.connect()

    try:
        result = await client.generate_video(
            prompt=(
                "Create a realistic 10-second vertical 9:16 cinematic "
                "video of a coffee shop at night."
            ),
            output_dir="outputs",
            timeout=600,
        )

        print("session_id:", result.session_id)
        print("videos:", result.videos)
        print("downloaded:", result.downloaded)

        for warning in result.download_errors:
            print("download warning:", warning)
    finally:
        await client.close()


asyncio.run(main())
```

## Filesystem Upload

Large files are uploaded in 512 KiB chunks.

For multi-chunk uploads the client writes to a temporary sibling file first:

```text
.upload-<uuid>.part
       ↓
fs.stat
       ↓
size validation
       ↓
fs.rename
       ↓
final destination
```

Example:

```bat
muse-ai upload "D:\file.bin" "workspace/user/files/file.bin"
```

Image attachments used by `chat.stream` do not use this upload path. They are sent inline as base64 data.

## Architecture

```text
Muse.ai
  │
  ├── Native OTP authentication
  │
  ├── Session / cookie bootstrap
  │
  └── Hatch admission bootstrap
          │
          ▼
wss://hatch.metaaivm.com/v1/noise
          │
          ▼
Noise_XX_25519_AESGCM_SHA256
          │
          ▼
NoiseTransportFrame
          │
          ▼
ServiceRequest / ServiceFrame
          │
          ▼
ApplicationRequest
          │
          ├── /api/ping
          ├── /model
          ├── /chat/stream
          ├── /chat/history
          ├── /api/session/list
          ├── /fs/*
          └── /api/idea-cards/media/*
```

RPC requests are multiplexed by `stream_id`, allowing history, media, and filesystem operations to share one encrypted Hatch connection.

## Protocol

Noise suite:

```text
Noise_XX_25519_AESGCM_SHA256
```

The implementation includes:

- X25519 key exchange
- AES-GCM transport encryption
- SHA-256 hashing
- Noise HKDF state derivation
- protobuf wire encoding/decoding
- encrypted frame fragmentation and reassembly
- application stream multiplexing

## Development

Run the test suite:

```bash
pytest -q
```

Compile-check the package:

```bash
python -m compileall muse_ai tests
```

## Security

This repository does not contain your Muse credentials.

The following paths are excluded from Git:

```text
.muse-state/
.venv/
outputs/
__pycache__/
.pytest_cache/
*.egg-info/
```

Do not commit `cookies.json`, OTP values, session tokens, HAR files containing active credentials, or generated private media.

## Limitations

This project uses an unofficial private Muse/Hatch protocol and may require updates when Muse changes its web client or backend protocol.

The captured Muse client contains confidential-VM/SNP attestation verification logic. This implementation currently decrypts the standard-VM handshake flow but does not independently provide the full browser-equivalent confidential-VM attestation verification chain.

## Disclaimer

Use this project only with accounts and data you are authorized to access, and follow the applicable Muse.ai terms and service limits.

## License

No license has been selected yet.
