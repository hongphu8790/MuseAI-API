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
from .errors import MuseVideoNotCreated
from .media import (
    ImageRef,
    VideoRef,
    extract_image_refs,
    extract_session_ids,
    extract_video_refs,
)
from .routes import CHAT_CAPABILITIES
from .text import (
    coalesce_text_fragments,
    assistant_response_finished,
    muse_video_task_pending,
    muse_video_task_terminal,
    terminal_video_task_ids,
    latest_assistant_text,
    extract_assistant_texts,
    extract_text_fragments,
)
from .transport import HatchConnection, RpcResponse


@dataclass(slots=True)
class GenerationResult:
    session_id: str | None
    videos: list[VideoRef]
    downloaded: list[Path] = field(default_factory=list)
    download_errors: list[str] = field(default_factory=list)
    stream_events: list[dict] = field(default_factory=list)
    history: dict | list | None = None


@dataclass(slots=True)
class TextResult:
    session_id: str | None
    text: str
    stream_events: list[dict] = field(default_factory=list)
    history: dict | list | None = None
    finished: bool = False
    completion_source: str = "unknown"


@dataclass(slots=True)
class ImageGenerationResult:
    session_id: str | None
    images: list[ImageRef]
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
            proxy=getattr(self.auth, "proxy", None),
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
        # Noise's assembled stream payload must stay below its size budget.
        inline_bytes = sum(
            len(item.get("data_base64", ""))
            for item in payload["items"] if item.get("type") == "image"
        )
        if inline_bytes > 12 * 1024 * 1024:
            raise ValueError(
                "Tổng dung lượng ảnh đính kèm sau xử lý quá lớn (giới hạn 12 MB mã hóa)."
            )

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

    async def send_text(
        self,
        *,
        prompt: str,
        session_id: str | None = None,
        timeout: float = 120.0,
        poll_interval: float = 1.5,
        settle_seconds: float = 8.0,
        attachments: list[dict] | None = None,
    ) -> TextResult:
        """Send text and wait for Muse's response to stop changing.

        History may expose a partial assistant message while Muse is still
        generating it, so the first non-empty history result is not final.
        """

        prompt = prompt.strip()
        if not prompt:
            raise ValueError("text prompt must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")

        deadline = time.monotonic() + timeout

        # A new chat is a real Muse secondary thread, not the primary inbox.
        # Omitting session_id from /chat/stream routes to the primary thread.
        # Muse accepts a fresh UUID as session_id and registers is_thread=True.
        target_session_id = session_id or str(uuid.uuid4())
        sessions_before: set[str] = set()

        baseline_history = None
        baseline_assistant: set[str] = set()
        try:
            baseline_history = await self.history(
                session_id=target_session_id,
                limit=80,
            )
            baseline_assistant = set(
                extract_assistant_texts(baseline_history)
            )
        except Exception:
            pass

        incoming = attachments or []
        images = [Path(item["path"]) for item in incoming if item["kind"] == "image"]
        references = []
        for item in incoming:
            if item["kind"] != "document":
                continue
            # Muse's captured web image payload format is known; document
            # attachment items are not. Upload docs to Muse workspace via its
            # fs API and describe the paths rather than guessing a binary schema.
            remote = (
                "workspace/.muse_chat_uploads/"
                + uuid.uuid4().hex + "/" + Path(item["name"]).name
            )
            uploaded = await self._fs().upload_file(item["path"], remote)
            references.append(f"- {item['name']}: {uploaded}")
        wire_prompt = prompt
        if references:
            wire_prompt += (
                "\n\nCác tài liệu đã tải lên workspace Muse. Hãy sử dụng chúng "
                "khi trả lời nếu có khả năng truy cập:\n" +
                "\n".join(references)
            )
        remaining = max(1.0, deadline - time.monotonic())
        stream_options = {
            "prompt": wire_prompt,
            "session_id": target_session_id,
            "stream_timeout": min(30.0, remaining),
        }
        if images:
            stream_options["images"] = images
        events = await self.chat_stream(**stream_options)

        event_sessions = extract_session_ids(events)
        if event_sessions and target_session_id not in event_sessions:
            raise RuntimeError(
                "Muse responded with a different session_id than the "
                "requested chat thread; refusing to mix conversations"
            )
        resolved_session = target_session_id

        event_fragments = [
            fragment
            for fragment in extract_assistant_texts(events)
            if fragment.strip() != prompt
        ]
        if not event_fragments:
            event_fragments = [
                fragment
                for fragment in extract_text_fragments(events)
                if fragment.strip() != prompt
            ]

        latest_history = baseline_history
        latest_response: str | None = None
        response_changed_at: float | None = None

        while time.monotonic() < deadline:
            try:
                latest_history = await self.history(
                    session_id=resolved_session,
                    limit=80,
                )
                assistant_texts = [
                    text
                    for text in extract_assistant_texts(latest_history)
                    if text not in baseline_assistant
                    and text.strip() != prompt
                ]
                if assistant_texts:
                    response = assistant_texts[-1]
                    if response != latest_response:
                        latest_response = response
                        response_changed_at = time.monotonic()
                    if assistant_response_finished(latest_history):
                        return TextResult(
                            session_id=resolved_session,
                            text=response,
                            stream_events=events,
                            history=latest_history,
                            finished=True,
                            completion_source="muse_final",
                        )
                    # Quiet text does NOT mean Muse has completed its reply.
                    # Return a provisional result; the media watcher can
                    # continue polling and must not call it a final refusal.
                    if settle_seconds <= 0 or (
                        response_changed_at is not None
                        and time.monotonic() - response_changed_at >= settle_seconds
                    ):
                        return TextResult(
                            session_id=resolved_session,
                            text=response,
                            stream_events=events,
                            history=latest_history,
                            finished=False,
                            completion_source="idle_fallback",
                        )
            except Exception:
                # A valid stream response remains usable if history is
                # temporarily unavailable.
                pass

            await asyncio.sleep(
                min(
                    poll_interval,
                    max(0.05, deadline - time.monotonic()),
                )
            )

        # A slow Muse response may still be changing at our deadline.
        # Return the newest observed snapshot rather than the first fragment.
        if latest_response:
            return TextResult(
                session_id=resolved_session,
                text=latest_response,
                stream_events=events,
                history=latest_history,
            )

        stream_text = coalesce_text_fragments(event_fragments)
        if stream_text:
            return TextResult(
                session_id=resolved_session,
                text=stream_text,
                stream_events=events,
                history=latest_history,
            )

        raise TimeoutError(
            f"No Muse text response appeared within {timeout:.0f}s"
            + (
                f" for session {resolved_session}"
                if resolved_session
                else ""
            )
        )

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
            # Never claim an arbitrary existing session (often the primary
            # conversation) as the result of a new chat request.
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
        baseline_assistant: set[str] | None = None,
        baseline_terminal: set[str] | None = None,
    ) -> tuple[list[VideoRef], dict | list | None]:
        deadline = time.monotonic() + timeout
        first_found_at: float | None = None
        latest_history = None
        latest_refs: list[VideoRef] = []
        final_without_video_polls = 0

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

            # The last assistant response has to be both NEW and explicitly
            # finished. A background video task with a pending/running status
            # takes precedence over a finalized assistant chat message.
            final_text = latest_assistant_text(latest_history)
            new_reply = (
                bool(final_text)
                and final_text not in (baseline_assistant or set())
            )
            if (
                not latest_refs
                and new_reply
                and assistant_response_finished(latest_history)
                and bool(
                    terminal_video_task_ids(latest_history) - (baseline_terminal or set())
                )
                and not muse_video_task_pending(latest_history)
            ):
                final_without_video_polls += 1
                if final_without_video_polls >= 2:
                    raise MuseVideoNotCreated(final_text, session_id=session_id)
            else:
                final_without_video_polls = 0

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
        baseline_assistant = set(extract_assistant_texts(baseline_history))
        baseline_terminal = terminal_video_task_ids(baseline_history)

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
            baseline_assistant=baseline_assistant,
            baseline_terminal=baseline_terminal,
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
                    # Media presentations sometimes arrive before their
                    # backing /fs/raw objects. Retry one 404 before recording
                    # the failed ref; other videos may already be available.
                    if "not_found" in str(exc) or "HTTP 404" in str(exc):
                        await asyncio.sleep(2.0)
                        try:
                            downloaded.append(
                                await self.download_video(ref, out / filename)
                            )
                            continue
                        except Exception as retry_exc:
                            exc = retry_exc
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

    async def wait_for_images(
        self,
        *,
        session_id: str | None,
        baseline: set[str],
        timeout: float = 300.0,
        poll_interval: float = 2.0,
        settle_seconds: float = 2.0,
        min_images: int = 1,
    ) -> tuple[list[ImageRef], dict | list | None]:
        deadline = time.monotonic() + timeout
        first_found_at: float | None = None
        latest_history = None
        latest_refs: list[ImageRef] = []

        while time.monotonic() < deadline:
            latest_history = await self.history(session_id=session_id, limit=80)
            refs = [
                ref
                for ref in extract_image_refs(latest_history)
                if ref.identity not in baseline
            ]
            unique = {ref.identity: ref for ref in refs}
            latest_refs = list(unique.values())

            if len(latest_refs) >= min_images:
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
            f"No new Muse image appeared within {timeout:.0f}s"
            + (f" for session {session_id}" if session_id else "")
        )

    @staticmethod
    def _image_filename_for(ref: ImageRef, index: int) -> str:
        for candidate in (ref.label, ref.path, ref.url):
            if not candidate:
                continue
            name = Path(urlparse(candidate).path).name
            if name and "." in name:
                return name

        mime = (ref.mime_type or "").lower()
        extension = {
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/webp": ".webp",
            "image/gif": ".gif",
            "image/avif": ".avif",
        }.get(mime, ".png")
        return f"muse_image_{index + 1}{extension}"

    async def download_image(
        self,
        ref: ImageRef,
        destination: str | Path,
    ) -> Path:
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        attempts: list[str] = []

        async def write_bytes(data: bytes) -> Path:
            target.write_bytes(data)
            return target

        if ref.url and "/idea-cards/media/" in ref.url:
            try:
                return await write_bytes(
                    await self._fs().idea_media_from_url(ref.url)
                )
            except Exception as exc:
                attempts.append(f"variants.original idea-media: {exc}")

        if ref.media_handle:
            try:
                return await write_bytes(
                    await self._fs().idea_media(ref.media_handle)
                )
            except Exception as exc:
                attempts.append(f"media_handle: {exc}")

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

        if ref.url and ref.url.startswith("sandbox://"):
            try:
                return await write_bytes(await self._fs().raw(ref.url))
            except Exception as exc:
                attempts.append(f"variants.original fs.raw: {exc}")

        if ref.path:
            try:
                return await self._fs().download(ref.path, target)
            except Exception as exc:
                attempts.append(f"path fs.raw: {exc}")

        detail = "; ".join(attempts) if attempts else "no usable media source"
        raise RuntimeError(f"unable to download generated image: {detail}")

    async def generate_image(
        self,
        *,
        prompt: str,
        images: list[str | Path] | None = None,
        output_dir: str | Path = "outputs",
        session_id: str | None = None,
        timeout: float = 300.0,
        min_images: int = 1,
        download: bool = True,
    ) -> ImageGenerationResult:
        """Generate image media through the same Muse chat stream.

        Muse's web client represents generated images as presentation kind
        "image" with data.images[] media records. The prompt is sent unchanged;
        callers should explicitly ask Muse to create/generate an image.
        """

        if not prompt.strip():
            raise ValueError("image generation requires a non-empty prompt")
        if min_images < 1:
            raise ValueError("min_images must be at least 1")

        sessions_before: set[str] = set()
        if session_id is None:
            try:
                sessions_before = set(
                    extract_session_ids(await self.sessions_list())
                )
            except Exception:
                pass

        baseline_history = await self.history(
            session_id=session_id,
            limit=80,
        )
        baseline = {
            ref.identity for ref in extract_image_refs(baseline_history)
        }

        events = await self.chat_stream(
            prompt=prompt,
            images=list(images or []),
            session_id=session_id,
        )
        resolved_session = await self._resolve_session_id(
            session_id,
            events,
            sessions_before,
        )

        images, latest_history = await self.wait_for_images(
            session_id=resolved_session,
            baseline=baseline,
            timeout=timeout,
            min_images=min_images,
        )

        downloaded: list[Path] = []
        download_errors: list[str] = []
        if download:
            out = Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)
            used: set[str] = set()

            for index, ref in enumerate(images):
                filename = self._image_filename_for(ref, index)
                original = filename
                suffix = 1
                while filename in used or (out / filename).exists():
                    stem = Path(original).stem
                    ext = Path(original).suffix or ".png"
                    filename = f"{stem}_{suffix}{ext}"
                    suffix += 1
                used.add(filename)

                try:
                    downloaded.append(
                        await self.download_image(ref, out / filename)
                    )
                except Exception as exc:
                    download_errors.append(
                        f"image[{index + 1}] {ref.identity}: {exc}"
                    )

        return ImageGenerationResult(
            session_id=resolved_session,
            images=images,
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
