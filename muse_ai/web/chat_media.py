from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..media import ImageRef, VideoRef, extract_image_refs, extract_video_refs
from ..text import (
    assistant_response_finished, latest_assistant_text,
    muse_video_task_pending, muse_video_task_terminal,
    terminal_video_task_ids,
)


@dataclass(frozen=True, slots=True)
class FileRef:
    path: str
    mime_type: str | None = None

    @property
    def identity(self) -> str:
        return self.path


def extract_file_refs(data: Any) -> list[FileRef]:
    """Collect explicitly typed Muse file attachments, excluding image/video."""
    found: dict[str, FileRef] = {}

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        kind = node.get("kind") or node.get("type")
        path = node.get("path")
        mime = node.get("mime_type") or node.get("mimeType")
        if (
            kind in {"file", "file_ref", "attachment"}
            and isinstance(path, str)
            and path.startswith(("sandbox://workspace/", "/workspace/", "workspace/"))
            and not (isinstance(mime, str) and mime.startswith(("image/", "video/")))
            and Path(path.split("?", 1)[0]).suffix.lower()
            in {".pdf", ".txt", ".csv", ".docx", ".xlsx", ".pptx", ".zip", ".json", ".md"}
        ):
            found[path] = FileRef(path=path, mime_type=mime if isinstance(mime, str) else None)
        for value in node.values():
            if isinstance(value, (dict, list)):
                walk(value)

    walk(data)
    return list(found.values())


def safe_media_name(name: str, default: str) -> str:
    name = Path(name.replace("\\", "/")).name
    cleaned = re.sub(r"[^\w. -]+", "_", name, flags=re.UNICODE).strip(" .")
    return cleaned[:145] or default


def media_references(data: Any) -> list[tuple[str, ImageRef | VideoRef | FileRef]]:
    refs: list[tuple[str, ImageRef | VideoRef | FileRef]] = []
    known: set[str] = set()
    for kind, found in (
        ("image", extract_image_refs(data)),
        ("video", extract_video_refs(data)),
        ("file", extract_file_refs(data)),
    ):
        for ref in found:
            key = f"{kind}:{ref.identity}"
            if key not in known:
                known.add(key)
                refs.append((kind, ref))
    return refs


@dataclass(slots=True)
class ChatMediaJob:
    id: str
    session_id: str
    status: str = "watching"
    files: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    error_code: str | None = None
    muse_message: str | None = None
    reply_finished: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "status": self.status,
            "files": self.files,
            "errors": self.errors,
            "error_code": self.error_code,
            "muse_message": self.muse_message,
            "reply_finished": self.reply_finished,
        }


class ChatMediaManager:
    """Keep media private and serve only files successfully downloaded locally."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, ChatMediaJob] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(4)
        for manifest in self.root.glob("*/manifest.json"):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                job = ChatMediaJob(
                    id=manifest.parent.name,
                    session_id=data["session_id"],
                    files=data.get("files", []),
                    errors=data.get("errors", []),
                    error_code=data.get("error_code"),
                    muse_message=data.get("muse_message"),
                    reply_finished=bool(data.get("reply_finished")),
                    status="interrupted" if data.get("status") == "watching" else data.get("status", "completed"),
                )
                self.jobs[job.id] = job
            except (ValueError, KeyError, TypeError, OSError):
                continue

    def persist(self, job: ChatMediaJob) -> None:
        folder = self.root / job.id
        folder.mkdir(parents=True, exist_ok=True)
        data = {"session_id": job.session_id, **job.public()}
        (folder / "manifest.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

    def get(self, media_id: str) -> ChatMediaJob | None:
        return self.jobs.get(media_id)

    def file_path(self, media_id: str, filename: str) -> Path | None:
        job = self.get(media_id)
        if not job or filename not in {item["filename"] for item in job.files}:
            return None
        path = (self.root / media_id / filename)
        return path if path.is_file() else None

    def start(
        self,
        *,
        session_id: str,
        baseline: set[str],
        fetch: Callable[[str], Awaitable[Any]],
        download: Callable[[str, ImageRef | VideoRef | FileRef, Path], Awaitable[Path]],
        timeout: float = 300,
        poll_interval: float = 3,
        settle_seconds: float = 12,
        response_text: str = "",
        expected_kind: str | None = None,
        baseline_terminal: set[str] | None = None,
    ) -> ChatMediaJob:
        media_id = uuid.uuid4().hex[:16]
        job = ChatMediaJob(id=media_id, session_id=session_id)
        self.jobs[media_id] = job
        self.persist(job)
        task = asyncio.create_task(
            self._run(
                job, baseline, fetch, download, timeout, poll_interval,
                settle_seconds, response_text, expected_kind, baseline_terminal or set(),
            ),
            name=f"muse-chat-media-{media_id}",
        )
        self.tasks[media_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(media_id, None))
        return job

    async def _run(
        self, job: ChatMediaJob, baseline: set[str],
        fetch: Callable[[str], Awaitable[Any]],
        download: Callable[[str, ImageRef | VideoRef | FileRef, Path], Awaitable[Path]],
        timeout: float, poll_interval: float, settle_seconds: float,
        response_text: str, expected_kind: str | None,
        baseline_terminal: set[str],
    ) -> None:
        seen = set(baseline)
        retries: dict[str, int] = {}
        first_file_time: float | None = None
        final_without_media_polls = 0
        deadline = time.monotonic() + timeout
        async with self._semaphore:
            try:
                while time.monotonic() < deadline:
                    try:
                        history = await fetch(job.session_id)
                    except Exception:
                        await asyncio.sleep(min(poll_interval, max(0, deadline - time.monotonic())))
                        continue

                    for index, (kind, ref) in enumerate(media_references(history)):
                        key = f"{kind}:{ref.identity}"
                        if key in seen:
                            continue
                        if retries.get(key, 0) >= 3:
                            continue
                        ext = (
                            Path((ref.path or ref.url or "").split("?", 1)[0]).suffix
                            or mimetypes.guess_extension(getattr(ref, "mime_type", "") or "")
                            or (".mp4" if kind == "video" else ".png")
                        )
                        if len(ext) > 9 or not re.fullmatch(r"\.[A-Za-z0-9]+", ext):
                            ext = ".mp4" if kind == "video" else ".png"
                        filename = safe_media_name(
                            f"{kind}-{len(job.files)+1}-{uuid.uuid4().hex[:7]}{ext}",
                            f"{kind}-{len(job.files)+1}{ext}",
                        )
                        target = self.root / job.id / filename
                        try:
                            result = await download(kind, ref, target)
                            if not result.is_file():
                                raise FileNotFoundError("Muse media was not saved")
                            media_type = getattr(ref, "mime_type", None) or mimetypes.guess_type(filename)[0]
                            if kind == "video" and not (media_type or "").startswith("video/"):
                                media_type = "video/mp4"
                            elif kind == "image" and not (media_type or "").startswith("image/"):
                                media_type = "image/png"
                            elif kind == "file":
                                media_type = media_type or "application/octet-stream"
                            base = f"/api/chat/media/{job.id}/files/{filename}"
                            job.files.append({
                                "kind": kind,
                                "filename": filename,
                                "mime_type": media_type,
                                "url": base,
                                "download_url": base + "?download=1",
                            })
                            seen.add(key)
                            first_file_time = time.monotonic()
                            self.persist(job)
                        except Exception:
                            retries[key] = retries.get(key, 0) + 1
                            if retries[key] >= 3:
                                seen.add(key)
                                job.errors.append(f"Không tải được tệp {kind} #{index+1} từ Muse.")
                                self.persist(job)

                    has_expected_media = (
                        any(f["kind"] == expected_kind for f in job.files)
                        if expected_kind else bool(job.files)
                    )
                    latest_text = latest_assistant_text(history)
                    same_response = (
                        bool(response_text)
                        and bool(latest_text)
                        and (latest_text.startswith(response_text)
                             or response_text.startswith(latest_text))
                    )
                    if same_response:
                        job.muse_message = max(
                            job.muse_message or response_text, latest_text, key=len
                        )
                        job.reply_finished = assistant_response_finished(history)
                        self.persist(job)
                    if (
                        expected_kind == "video"
                        and not has_expected_media
                        and same_response
                        and assistant_response_finished(history)
                        and bool(terminal_video_task_ids(history) - baseline_terminal)
                        and not muse_video_task_pending(history)
                        and not any(kind == "video" and
                                    f"video:{ref.identity}" not in baseline
                                    for kind, ref in media_references(history))
                    ):
                        final_without_media_polls += 1
                        if final_without_media_polls >= 2:
                            job.status = "no_media"
                            job.error_code = "muse_finished_without_video"
                            job.muse_message = latest_text
                            job.errors.append(
                                "Muse đã trả lời xong nhưng không có video. "
                                "Phản hồi Muse: " + latest_text
                            )
                            break
                    else:
                        final_without_media_polls = 0

                    if first_file_time is not None and time.monotonic() - first_file_time >= settle_seconds:
                        break
                    if expected_kind is None and same_response and job.reply_finished:
                        # A normal chat response with no requested video is
                        # successful once Muse confirms its final message.
                        break
                    await asyncio.sleep(min(poll_interval, max(0, deadline - time.monotonic())))

                if job.status == "no_media":
                    pass
                elif expected_kind == "video" and not any(
                    f["kind"] == "video" for f in job.files
                ):
                    job.status = "failed" if job.errors else "no_media"
                    if not job.errors:
                        job.errors.append("Hết thời gian theo dõi nhưng Muse chưa cung cấp video.")
                elif job.files or (expected_kind is None and job.reply_finished):
                    job.status = "completed"
                elif job.errors:
                    job.status = "failed"
                elif expected_kind is None:
                    job.status = "reply_unconfirmed"
                    job.errors.append(
                        "Chưa xác minh được tín hiệu hoàn tất của Muse; "
                        "có thể đồng bộ lại lịch sử theo session_id."
                    )
                else:
                    job.status = "no_media"
                    job.errors.append("Muse chưa trả về tệp nào trong thời gian theo dõi.")
            except asyncio.CancelledError:
                job.status = "interrupted"
                raise
            except Exception:
                job.status = "failed"
                job.errors.append("Không thể theo dõi tệp Muse.")
            finally:
                self.persist(job)

    async def close(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
