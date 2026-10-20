from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .auth import MuseAuth
from .client import MuseClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="muse-ai",
        description="Unofficial Muse.ai Hatch/Noise Python client",
    )
    parser.add_argument("--state-dir", default=".muse-state")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="Login with Muse OTP and save cookies")
    login.add_argument("--email", required=True)
    login.add_argument("--region", default="VN")

    sub.add_parser("ping", help="Connect to Hatch and call /api/ping")
    sub.add_parser("model", help="Read the active Muse model")

    history = sub.add_parser("history", help="Read recent chat history")
    history.add_argument("--session-id")
    history.add_argument("--limit", type=int, default=40)

    generate = sub.add_parser(
        "generate",
        help="Generate video from a text prompt, optionally with reference images",
    )
    generate.add_argument("--prompt", required=True)
    generate.add_argument(
        "--image",
        action="append",
        default=[],
        help="Optional reference image; repeat --image for multiple images",
    )
    generate.add_argument("--output", default="outputs")
    generate.add_argument("--session-id")
    generate.add_argument("--timeout", type=float, default=600)
    generate.add_argument("--min-videos", type=int, default=1)
    generate.add_argument("--no-download", action="store_true")

    upload = sub.add_parser("upload", help="Test the reverse-engineered fs.write upload")
    upload.add_argument("local_file")
    upload.add_argument("remote_path")

    return parser


async def _connect(args) -> MuseClient:
    auth = MuseAuth(state_dir=args.state_dir)
    client = MuseClient(auth, state_dir=args.state_dir)
    await client.connect()
    return client


async def run(args: argparse.Namespace) -> int:
    if args.command == "login":
        auth = MuseAuth(state_dir=args.state_dir)
        try:
            await auth.login_interactive(args.email, region=args.region)
            print(f"Login validated. Saved session in {auth.cookie_path}")
            return 0
        finally:
            await auth.close()

    client = await _connect(args)
    try:
        if args.command == "ping":
            response = await client.ping()
            print(f"HTTP {response.status}")
            print(response.text())
        elif args.command == "model":
            print(json.dumps(await client.model_get(), ensure_ascii=False, indent=2))
        elif args.command == "history":
            value = await client.history(
                session_id=args.session_id,
                limit=args.limit,
            )
            print(json.dumps(value, ensure_ascii=False, indent=2))
        elif args.command == "upload":
            remote = await client._fs().upload_file(
                args.local_file,
                args.remote_path,
                stage_before_publish=True,
            )
            print(remote)
        elif args.command == "generate":
            result = await client.generate_video(
                prompt=args.prompt,
                images=[Path(value) for value in args.image] if args.image else [],
                output_dir=args.output,
                session_id=args.session_id,
                timeout=args.timeout,
                min_videos=args.min_videos,
                download=not args.no_download,
            )
            print(f"session_id={result.session_id}")
            for index, ref in enumerate(result.videos, start=1):
                print(
                    f"video[{index}] path={ref.path!r} "
                    f"url={ref.url!r} media_handle={ref.media_handle!r}"
                )
            for path in result.downloaded:
                print(f"downloaded: {path}")
            for error in result.download_errors:
                print(f"download warning: {error}")
        return 0
    finally:
        await client.close()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
