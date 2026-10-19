"""Validation and durable staging of local files attached to Muse chat."""
from __future__ import annotations

import re
import uuid
from pathlib import Path

from fastapi import UploadFile

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
DOCUMENT_SUFFIXES = {
    ".pdf", ".txt", ".md", ".csv", ".json", ".docx", ".xlsx", ".pptx",
}
ALLOWED_SUFFIXES = IMAGE_SUFFIXES | DOCUMENT_SUFFIXES
MAX_FILES = 10
MAX_SINGLE_SIZE = 12 * 1024 * 1024
MAX_TOTAL_SIZE = 40 * 1024 * 1024


def safe_upload_name(name: str | None) -> str:
    # User-provided filenames must never be used as paths.
    basename = (name or "file").replace("\\", "/").split("/")[-1]
    clean = re.sub(r"[^\w.\- ]", "_", basename, flags=re.UNICODE).strip(" .")
    if not clean:
        raise ValueError("Tên tệp không hợp lệ")
    return clean[:120]


async def stage_chat_files(
    uploads: list[UploadFile], root: Path, thread_id: str,
) -> list[dict]:
    if len(uploads) > MAX_FILES:
        raise ValueError(f"Tối đa {MAX_FILES} tệp mỗi tin nhắn")
    if not uploads:
        return []
    if not re.fullmatch(r"[\w-]{1,128}", thread_id):
        raise ValueError("Thread ID không hợp lệ")
    folder = root / thread_id
    folder.mkdir(parents=True, exist_ok=True)
    staged: list[dict] = []
    used = 0
    try:
        for upload in uploads:
            name = safe_upload_name(upload.filename)
            suffix = Path(name).suffix.lower()
            if suffix not in ALLOWED_SUFFIXES:
                raise ValueError(f"Định dạng {suffix or 'không rõ'} không được hỗ trợ")
            ident = uuid.uuid4().hex
            destination = folder / (ident + suffix)
            size = 0
            try:
                with destination.open("xb") as handle:
                    while data := await upload.read(512 * 1024):
                        size += len(data)
                        used += len(data)
                        if size > MAX_SINGLE_SIZE or used > MAX_TOTAL_SIZE:
                            raise ValueError("Tệp vượt giới hạn 12 MB/tệp hoặc 40 MB/tin")
                        handle.write(data)
            finally:
                await upload.close()
            if size == 0:
                raise ValueError(f"Tệp trống: {name}")
            staged.append({
                "id": ident, "name": name, "size": size,
                "kind": "image" if suffix in IMAGE_SUFFIXES else "document",
                "path": str(destination),
            })
        return staged
    except BaseException:
        for entry in staged:
            Path(entry["path"]).unlink(missing_ok=True)
        # Remove the currently in-progress file too.
        if 'destination' in locals():
            destination.unlink(missing_ok=True)
        raise
