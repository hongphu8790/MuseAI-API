from __future__ import annotations

import base64
import io
import mimetypes
from pathlib import Path

from PIL import Image


def _mime_for(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    if mime and mime.startswith("image/"):
        return mime
    suffix = path.suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix, "application/octet-stream")


def _prepare_image_bytes(
    path: Path,
    *,
    max_dimension: int = 2048,
    target_bytes: int = 3_500_000,
) -> tuple[bytes, str, str]:
    raw = path.read_bytes()
    mime = _mime_for(path)
    if not mime.startswith("image/"):
        raise ValueError(f"{path} is not a supported image")

    # The Muse web client sends image attachments inline as base64.  Browser-side
    # preprocessing can shrink large images; mirror that behavior enough to keep
    # the whole chat.stream request inside Noise's ~16 MiB assembly budget.
    if len(raw) <= target_bytes:
        return raw, mime, path.name

    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        if max(image.size) > max_dimension:
            image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)

        has_alpha = image.mode in ("RGBA", "LA") or (
            image.mode == "P" and "transparency" in image.info
        )
        if has_alpha:
            out = io.BytesIO()
            image.save(out, format="WEBP", quality=88, method=6)
            data = out.getvalue()
            return data, "image/webp", path.stem + ".webp"

        image = image.convert("RGB")
        quality = 90
        data = b""
        while quality >= 55:
            out = io.BytesIO()
            image.save(out, format="JPEG", quality=quality, optimize=True)
            data = out.getvalue()
            if len(data) <= target_bytes:
                break
            quality -= 8
        return data, "image/jpeg", path.stem + ".jpg"


def build_image_item(path: str | Path) -> dict:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)
    data, mime, filename = _prepare_image_bytes(source)
    return {
        "type": "image",
        "mime_type": mime,
        "data_base64": base64.b64encode(data).decode("ascii"),
        "filename": filename,
    }


def build_items(images: list[str | Path], prompt: str) -> list[dict]:
    items = [build_image_item(path) for path in images]
    items.append({"type": "text", "text": prompt})
    return items
