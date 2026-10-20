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

## Weekly usage

```bash
muse-ai usage
```

prints the plan, the percent of the weekly allowance used, when it resets
and the quota status (Settings > General > Usage in the web app).
`muse-ai generate` prints the same line after every generation
(`--no-usage` turns it off). The web app reads it through the Next.js server
action `fetchSubscriptionAction`; if Meta changes that action, set
`MUSE_SUBSCRIPTION_ACTION` to the new id.

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
4. polls `/chat/history`;
5. detects new video media;
6. downloads the generated result.

If generation succeeds but a download source is temporarily unavailable, the client keeps the generation result and reports a download warning instead of treating the entire generation as failed.

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
