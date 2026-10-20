from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .attachments import build_items
from .auth import MuseAuth
from .filesystem import MuseFilesystem
from .media import VideoRef, extract_session_ids, extract_video_refs
from .routes import CHAT_CAPABILITIES
from .transport import HatchConnection, RpcResponse


@dataclass(slots=True)
class GenerationResult:
    session_id: str | None
    videos: list[VideoRef]
    downloaded: list[Path] = field(default_factory=list)
    download_errors: list[str] = field(default_factory=list)
    stream_events: list[dict] = field(default_factory=list)
    history: dict | list | None = None


class MuseClient:
    def __init__(
        self,
        auth: MuseAuth,
        *,
        state_dir: str | Path | None = None,
    ) -> None:
        self.auth = auth
        self.state_dir = Path(state_dir) if state_dir is not None else auth.state_dir
        self.conn: HatchConnection | None = None
        self.fs: MuseFilesystem | None = None

    async def connect(
        self,
        *,
        contact_point: str | None = None,
        region: str = "VN",
        otp_code: str | None = None,
    ) -> None:
        await self.auth.ensure_session(
            contact_point=contact_point,
            region=region,
            otp_code=otp_code,
        )
        bootstrap = await self.auth.bootstrap_hatch()
        self.conn = await HatchConnection.connect(
            bootstrap.websocket_url(),
            locale=bootstrap.locale,
        )
        self.fs = MuseFilesystem(self.conn)

    def _conn(self) -> HatchConnection:
        if self.conn is None:
            raise RuntimeError("MuseClient is not connected")
        return self.conn

    def _fs(self) -> MuseFilesystem:
        if self.fs is None:
            raise RuntimeError("MuseClient is not connected")
        return self.fs

    def device_id(self) -> str:
        path = self.state_dir / "device.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                value = data.get("device_id")
                if isinstance(value, str) and value:
                    return value
            except Exception:
                pass
        value = str(uuid.uuid4())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"device_id": value}, indent=2), encoding="utf-8")
        return value

    async def ping(self) -> RpcResponse:
        response = await self._conn().request("POST", "/api/ping", {})
        return response.raise_for_status()

    async def model_get(self):
        response = await self._conn().request("GET", "/model", {})
        response.raise_for_status()
        return response.json()

    async def sessions_list(self):
        response = await self._conn().request("GET", "/api/session/list", {})
        response.raise_for_status()
        return response.json()

    async def history(
        self,
        *,
        session_id: str | None = None,
        limit: int = 40,
        transcript_mode: str = "messages",
    ):
        params: dict = {"limit": limit, "transcript_mode": transcript_mode}
        if session_id:
            params["session_id"] = session_id
        response = await self._conn().request("GET", "/chat/history", params)
        response.raise_for_status()
        return response.json()

    async def chat_stream(
        self,
        *,
        prompt: str,
        images: list[str | Path] | None = None,
        session_id: str | None = None,
        stream_timeout: float = 30.0,
    ) -> list[dict]:
        payload = {
            "items": build_items(images or [], prompt),
            "node_id": self.device_id(),
            "capabilities": list(CHAT_CAPABILITIES),
        }
        if session_id:
            payload["session_id"] = session_id

        events: list[dict] = []
        try:
            async with asyncio.timeout(stream_timeout):
                async for event in self._conn().subscribe(
                    "POST",
                    "/chat/stream",
                    payload,
                ):
                    events.append(event)
        except TimeoutError:
            # chat.stream is an acknowledgement/subscription endpoint.  Some server
            # revisions leave it open longer than the generation itself; generation
            # tracking below does not depend on it remaining open.
            pass
        return events

    async def _resolve_session_id(
        self,
        explicit: str | None,
        events: list[dict],
        sessions_before: set[str],
    ) -> str | None:
        if explicit:
            return explicit
        event_ids = extract_session_ids(events)
        if event_ids:
            return event_ids[-1]
        try:
            sessions_after = await self.sessions_list()
            current = extract_session_ids(sessions_after)
            new_ids = [value for value in current if value not in sessions_before]
            if new_ids:
                return new_ids[0]
            if current:
                return current[0]
        except Exception:
            pass
        return None

    async def wait_for_videos(
        self,
        *,
        session_id: str | None,
        baseline: set[str],
        timeout: float = 600.0,
        poll_interval: float = 3.0,
        settle_seconds: float = 5.0,
        min_videos: int = 1,
    ) -> tuple[list[VideoRef], dict | list | None]:
        deadline = time.monotonic() + timeout
        first_found_at: float | None = None
        latest_history = None
        latest_refs: list[VideoRef] = []

        while time.monotonic() < deadline:
            latest_history = await self.history(session_id=session_id, limit=80)
            refs = [
                ref
                for ref in extract_video_refs(latest_history)
                if ref.identity not in baseline
            ]
            unique = {ref.identity: ref for ref in refs}
            latest_refs = list(unique.values())
            if len(latest_refs) >= min_videos:
                if first_found_at is None:
                    first_found_at = time.monotonic()
                if time.monotonic() - first_found_at >= settle_seconds:
                    return latest_refs, latest_history
            else:
                first_found_at = None
            await asyncio.sleep(poll_interval)

        if latest_refs:
            return latest_refs, latest_history
        raise TimeoutError(
            f"No new Muse video appeared within {timeout:.0f}s"
            + (f" for session {session_id}" if session_id else "")
        )

    @staticmethod
    def _filename_for(ref: VideoRef, index: int) -> str:
        candidate = ref.path
        if not candidate and ref.url:
            candidate = urlparse(ref.url).path
        if candidate:
            name = Path(candidate).name
            if name and "." in name:
                return name
        return f"muse_video_{index + 1}.mp4"

    async def download_video(
        self,
        ref: VideoRef,
        destination: str | Path,
    ) -> Path:
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        attempts: list[str] = []

        async def write_bytes(data: bytes) -> Path:
            target.write_bytes(data)
            return target

        # The frontend treats /idea-cards/media/<handle> as a Hatch RPC media
        # route, not a normal public HTTP URL.
        if ref.url and "/idea-cards/media/" in ref.url:
            try:
                return await write_bytes(
                    await self._fs().idea_media_from_url(ref.url)
                )
            except Exception as exc:
                attempts.append(f"variants.original idea-media: {exc}")

        # Some presentations expose the opaque media handle separately.
        if ref.media_handle:
            try:
                return await write_bytes(
                    await self._fs().idea_media(ref.media_handle)
                )
            except Exception as exc:
                attempts.append(f"media_handle: {exc}")

        # A true external/signed URL can be fetched directly.
        if ref.url and ref.url.startswith(("http://", "https://")):
            try:
                response = await self.auth.browser.get(
                    ref.url,
                    allow_redirects=True,
                )
                if response.status_code >= 400:
                    raise RuntimeError(
                        f"HTTP {response.status_code}: {response.text[:200]}"
                    )
                return await write_bytes(response.content)
            except Exception as exc:
                attempts.append(f"direct URL: {exc}")

        # variants.original can also be another sandbox workspace reference.
        if ref.url and ref.url.startswith("sandbox://"):
            try:
                return await write_bytes(await self._fs().raw(ref.url))
            except Exception as exc:
                attempts.append(f"variants.original fs.raw: {exc}")

        # Last fallback: the presentation's sandbox path.  This is not always
        # backed by /fs/raw, which is why media_handle is attempted first.
        if ref.path:
            try:
                return await self._fs().download(ref.path, target)
            except Exception as exc:
                attempts.append(f"path fs.raw: {exc}")

        detail = "; ".join(attempts) if attempts else "no usable media source"
        raise RuntimeError(f"unable to download generated video: {detail}")

    async def generate_video(
        self,
        *,
        prompt: str,
        images: list[str | Path] | None = None,
        output_dir: str | Path = "outputs",
        session_id: str | None = None,
        timeout: float = 600.0,
        min_videos: int = 1,
        download: bool = True,
    ) -> GenerationResult:
        if not prompt.strip():
            raise ValueError("video generation requires a non-empty prompt")

        images = images or []
        sessions_before: set[str] = set()
        if session_id is None:
            try:
                sessions_before = set(extract_session_ids(await self.sessions_list()))
            except Exception:
                pass

        baseline_history = await self.history(session_id=session_id, limit=80)
        baseline = {ref.identity for ref in extract_video_refs(baseline_history)}

        events = await self.chat_stream(
            prompt=prompt,
            images=images,
            session_id=session_id,
        )
        resolved_session = await self._resolve_session_id(
            session_id, events, sessions_before
        )

        videos, latest_history = await self.wait_for_videos(
            session_id=resolved_session,
            baseline=baseline,
            timeout=timeout,
            min_videos=min_videos,
        )

        downloaded: list[Path] = []
        download_errors: list[str] = []
        if download:
            out = Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)
            used: set[str] = set()
            for index, ref in enumerate(videos):
                filename = self._filename_for(ref, index)
                original = filename
                suffix = 1
                while filename in used or (out / filename).exists():
                    stem = Path(original).stem
                    ext = Path(original).suffix or ".mp4"
                    filename = f"{stem}_{suffix}{ext}"
                    suffix += 1
                used.add(filename)
                try:
                    downloaded.append(
                        await self.download_video(ref, out / filename)
                    )
                except Exception as exc:
                    download_errors.append(
                        f"video[{index + 1}] {ref.identity}: {exc}"
                    )

        return GenerationResult(
            session_id=resolved_session,
            videos=videos,
            downloaded=downloaded,
            download_errors=download_errors,
            stream_events=events,
            history=latest_history,
        )

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None
            self.fs = None
        await self.auth.close()

    async def __aenter__(self) -> "MuseClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()
