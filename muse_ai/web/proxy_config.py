"""Per-account proxy configuration; credentials never leave the local server."""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

SUPPORTED_SCHEMES = {"http", "https", "socks5", "socks5h"}


def validate_proxy(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    if any(ch.isspace() for ch in value):
        raise ValueError("Proxy URL must not contain whitespace")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in SUPPORTED_SCHEMES:
        raise ValueError("Use http://, https://, socks5:// or socks5h:// proxy URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Proxy port is invalid") from exc
    if not parsed.hostname or not port or not (1 <= port <= 65535):
        raise ValueError("Proxy requires a host and TCP port")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("Proxy URL cannot include a path, query or fragment")
    return value


def proxy_config_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / "proxy.json"


def load_proxy(state_dir: str | Path) -> str | None:
    path = proxy_config_path(state_dir)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return validate_proxy(data.get("url"))


def save_proxy(state_dir: str | Path, value: str | None) -> None:
    value = validate_proxy(value)
    path = proxy_config_path(state_dir)
    if value is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"url": value}, handle)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def public_proxy(state_dir: str | Path) -> dict:
    try:
        value = load_proxy(state_dir)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"proxy_enabled": False, "proxy_label": "Cấu hình proxy lỗi"}
    if not value:
        return {"proxy_enabled": False, "proxy_label": None}
    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    label = f"{parsed.scheme}://{hostname}:{parsed.port}"
    return {"proxy_enabled": True, "proxy_label": label}
