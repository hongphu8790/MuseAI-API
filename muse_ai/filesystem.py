from __future__ import annotations

import base64
import math
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlparse

from .transport import HatchConnection
from .wire import Header

UPLOAD_CHUNK_SIZE = 524_288


def normalize_gateway_path(value: str) -> str:
    text = value.strip()
    text = text.split("?", 1)[0].split("#", 1)[0]
    if text.startswith("file://"):
        text = text[7:]
    text = text.replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    text = text.lstrip("/")
    parts = [part for part in text.split("/") if part]
    if not parts:
        return ""
    if "workspace" in parts:
        parts = parts[parts.index("workspace"):]
    elif parts[0] in {"home", "Users"} and len(parts) >= 3:
        parts = parts[2:]
    return "/".join(parts)


class MuseFilesystem:
    def __init__(self, conn: HatchConnection) -> None:
        self.conn = conn

    async def stat(self, path: str) -> dict:
        response = await self.conn.request(
            "POST", "/fs/stat", {"path": normalize_gateway_path(path)}
        )
        response.raise_for_status()
        return response.json()

    async def list(self, path: str) -> dict:
        response = await self.conn.request(
            "POST", "/fs/list", {"path": normalize_gateway_path(path)}
        )
        response.raise_for_status()
        return response.json()

    async def write_chunk(
        self,
        path: str,
        data: bytes,
        *,
        overwrite: bool,
        append: bool,
        create_parent: bool,
    ) -> dict | None:
        response = await self.conn.request(
            "POST",
            "/fs/write",
            {
                "path": normalize_gateway_path(path),
                "data_base64": base64.b64encode(data).decode("ascii"),
                "overwrite": overwrite,
                "append": append,
                "create_parent": create_parent,
            },
            timeout=120,
        )
        response.raise_for_status()
        return response.json()

    async def rename(self, source: str, destination: str) -> dict | None:
        response = await self.conn.request(
            "POST",
            "/fs/rename",
            {
                "source": normalize_gateway_path(source),
                "destination": normalize_gateway_path(destination),
                "create_parent": True,
                "overwrite": True,
            },
        )
        response.raise_for_status()
        return response.json()

    async def delete(self, path: str, *, recursive: bool = False) -> dict | None:
        response = await self.conn.request(
            "POST",
            "/fs/delete",
            {"path": normalize_gateway_path(path), "recursive": recursive},
        )
        response.raise_for_status()
        return response.json()

    async def upload_file(
        self,
        local_path: str | Path,
        remote_path: str,
        *,
        stage_before_publish: bool = True,
    ) -> str:
        source = Path(local_path)
        size = source.stat().st_size
        destination = normalize_gateway_path(remote_path)
        total_chunks = math.ceil(size / UPLOAD_CHUNK_SIZE)
        staged = stage_before_publish and total_chunks > 1
        if "/" in destination:
            parent, name = destination.rsplit("/", 1)
            temp = f"{parent}/.upload-{uuid.uuid4()}.part"
        else:
            temp = f".upload-{uuid.uuid4()}.part"
        target = temp if staged else destination

        try:
            if size == 0:
                await self.write_chunk(
                    target,
                    b"",
                    overwrite=True,
                    append=False,
                    create_parent=True,
                )
            else:
                with source.open("rb") as handle:
                    index = 0
                    while True:
                        chunk = handle.read(UPLOAD_CHUNK_SIZE)
                        if not chunk:
                            break
                        await self.write_chunk(
                            target,
                            chunk,
                            overwrite=index == 0,
                            append=index != 0,
                            create_parent=index == 0,
                        )
                        index += 1

            if staged:
                info = await self.stat(temp)
                if info.get("kind") != "file" or int(info.get("size", -1)) != size:
                    raise RuntimeError("uploaded file failed size validation")
                await self.rename(temp, destination)
            return destination
        except BaseException:
            if staged:
                try:
                    await self.delete(temp)
                except Exception:
                    pass
            raise

    async def raw(
        self,
        path: str,
        *,
        byte_range: tuple[int, int] | None = None,
        timeout: float = 300,
    ) -> bytes:
        normalized = normalize_gateway_path(path)
        route = "/fs/raw/" + quote(normalized, safe="/")
        headers: list[Header] = []
        if byte_range is not None:
            start, end = byte_range
            if start < 0 or end < start:
                raise ValueError("invalid byte range")
            headers.append(Header("Range", f"bytes={start}-{end}"))
        response = await self.conn.request(
            "GET",
            route,
            {},
            extra_headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.body

    async def idea_media(
        self,
        handle: str,
        *,
        params: dict[str, str] | None = None,
        timeout: float = 300,
    ) -> bytes:
        route = "/api/idea-cards/media/" + quote(handle, safe="")
        response = await self.conn.request(
            "GET",
            route,
            params or {},
            timeout=timeout,
        )
        response.raise_for_status()
        return response.body

    async def idea_media_from_url(self, value: str, *, timeout: float = 300) -> bytes:
        parsed = urlparse(value)
        path = parsed.path if parsed.scheme else value.split("?", 1)[0]
        marker = "/idea-cards/media/"
        if marker not in path:
            raise ValueError("not an idea-cards media URL")
        handle = path.split(marker, 1)[1].strip("/")
        if not handle:
            raise ValueError("idea-cards media handle is empty")
        query = parsed.query if parsed.scheme else (
            value.split("?", 1)[1] if "?" in value else ""
        )
        params = dict(parse_qsl(query, keep_blank_values=True))
        return await self.idea_media(handle, params=params, timeout=timeout)

    async def download(self, path: str, destination: str | Path) -> Path:
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(await self.raw(path))
        return target
