from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


@dataclass(frozen=True, slots=True)
class VideoRef:
    path: str | None = None
    url: str | None = None
    mime_type: str | None = None
    resource_id: str | None = None
    media_handle: str | None = None

    @property
    def identity(self) -> str:
        # Signed URLs can rotate while the generated file itself is unchanged.
        return (
            self.path
            or self.resource_id
            or self.media_handle
            or self.url
            or repr(self)
        )


def _is_video_mime(value: Any) -> bool:
    return isinstance(value, str) and (
        value.lower().startswith("video/") or value.lower() == "mp4"
    )


def _looks_video_path(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    clean = value.lower().split("?", 1)[0].split("#", 1)[0]
    return clean.endswith((".mp4", ".webm", ".mov", ".m4v"))


def extract_session_ids(value: Any) -> list[str]:
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if key in {"session_id", "sessionId", "thread_id", "threadId"}:
                    if isinstance(item, str) and item and item not in found:
                        found.append(item)
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return found


def _file_name(value: str | None) -> str | None:
    """Last path component of a video path/URL (query and fragment dropped).
    Muse reports one generated file both as a bare name and as a
    sandbox:// path or signed URL; they must collapse to one VideoRef."""
    if not value:
        return None
    clean = value.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    name = clean.rsplit("/", 1)[-1]
    return name if _looks_video_path(name) else None


def _aliases(ref: VideoRef) -> set[str]:
    values = {v for v in (ref.path, ref.url, ref.resource_id, ref.media_handle) if v}
    values |= {f"file:{n}" for n in (_file_name(ref.path), _file_name(ref.url)) if n}
    return values


def extract_video_refs(value: Any) -> list[VideoRef]:
    found: dict[str, VideoRef] = {}

    def add(ref: VideoRef) -> None:
        if not (ref.path or ref.url or ref.media_handle):
            return

        aliases = _aliases(ref)
        for existing_key, existing in list(found.items()):
            if aliases & _aliases(existing):
                # prefer the reference that can actually be fetched: a URL /
                # sandbox path over a bare file name
                def pick(a: str | None, b: str | None) -> str | None:
                    if a and b and "/" not in a and "/" in b:
                        return b
                    return a or b
                merged = VideoRef(
                    path=pick(ref.path, existing.path),
                    url=ref.url or existing.url,
                    mime_type=ref.mime_type or existing.mime_type,
                    resource_id=ref.resource_id or existing.resource_id,
                    media_handle=ref.media_handle or existing.media_handle,
                )
                del found[existing_key]
                found[merged.identity] = merged
                return

        found[ref.identity] = ref

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            mime = (
                node.get("mime_type")
                or node.get("mimeType")
                or node.get("mime")
                or node.get("content_type")
                or node.get("contentType")
            )
            kind = node.get("kind") or node.get("type")
            path = node.get("path")
            url = node.get("url")
            variants = node.get("variants")
            original = variants.get("original") if isinstance(variants, dict) else None
            resource_id = node.get("resource_id") or node.get("resourceId")
            media_handle = node.get("media_handle") or node.get("mediaHandle")
            videoish = (
                kind == "video"
                or _is_video_mime(mime)
                or _looks_video_path(path)
                or _looks_video_path(url)
                or _looks_video_path(original)
            )
            if videoish:
                add(
                    VideoRef(
                        path=path if isinstance(path, str) else None,
                        url=(
                            original
                            if isinstance(original, str)
                            else url if isinstance(url, str) else None
                        ),
                        mime_type=mime if isinstance(mime, str) else None,
                        resource_id=(
                            resource_id if isinstance(resource_id, str) else None
                        ),
                        media_handle=(
                            media_handle if isinstance(media_handle, str) else None
                        ),
                    )
                )
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            stripped = node.strip()
            if stripped.startswith(("{", "[")):
                try:
                    walk(json.loads(stripped))
                    return
                except (ValueError, TypeError):
                    pass
            if _looks_video_path(node):
                if node.startswith(("http://", "https://")):
                    add(VideoRef(url=node))
                else:
                    add(VideoRef(path=node))

    walk(value)
    return list(found.values())
