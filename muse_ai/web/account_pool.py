from __future__ import annotations

import asyncio
import json
import random
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..auth import MuseAuth
from .proxy_config import public_proxy, save_proxy


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AccountRecord:
    id: str
    email: str
    label: str
    enabled: bool = True
    status: str = "needs_login"
    created_at: str = field(default_factory=now)
    last_verified_at: str | None = None
    last_error: str | None = None

    def public(self, busy: bool = False) -> dict[str, Any]:
        return {**asdict(self), "busy": busy}


class AccountPool:
    """Isolated per-account Muse sessions; one concurrent generation per account."""

    def __init__(self, root: Path, legacy_state: Path | None = None) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.metadata_path = root / "accounts.json"
        self._lock = asyncio.Lock()
        self._accounts: dict[str, AccountRecord] = {}
        self._pending_auth: dict[str, MuseAuth] = {}
        self._leases: set[str] = set()
        self._load()
        if not self._accounts and legacy_state and (legacy_state / "cookies.json").exists():
            account_id = uuid.uuid4().hex
            directory = self.state_dir(account_id)
            directory.mkdir(parents=True, exist_ok=True)
            for name in ("cookies.json", "device.json"):
                source = legacy_state / name
                if source.is_file():
                    shutil.copy2(source, directory / name)
            self._accounts[account_id] = AccountRecord(
                id=account_id, email="", label="Tài khoản Muse cũ",
                status="stored",
            )
            self._persist()

    def state_dir(self, account_id: str) -> Path:
        return self.root / account_id

    def _load(self) -> None:
        if not self.metadata_path.exists():
            return
        try:
            data = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError("Invalid accounts index")
        except (OSError, ValueError, TypeError):
            return
        for entry in data:
            fields = {key: value for key, value in entry.items()
                      if key in AccountRecord.__dataclass_fields__}
            record = AccountRecord(**fields)
            record.status = (
                "stored" if (self.state_dir(record.id) / "cookies.json").exists()
                else "needs_login"
            )
            self._accounts[record.id] = record

    def _persist(self) -> None:
        temp = self.metadata_path.with_suffix(".tmp")
        temp.write_text(
            json.dumps([asdict(record) for record in self._accounts.values()],
                       ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.replace(self.metadata_path)

    def get(self, account_id: str) -> AccountRecord:
        account = self._accounts.get(account_id)
        if account is None:
            raise KeyError("Muse account not found")
        return account

    def list(self) -> list[dict[str, Any]]:
        return [
            {**account.public(account.id in self._leases),
             **public_proxy(self.state_dir(account.id))}
            for account in self._accounts.values()
        ]

    async def set_proxy(self, account_id: str, proxy_url: str | None) -> dict[str, Any]:
        async with self._lock:
            record = self.get(account_id)
            if self.is_busy(account_id):
                raise RuntimeError("Không đổi proxy khi tài khoản đang chạy task.")
            save_proxy(self.state_dir(account_id), proxy_url)
            # A new verification will use the configured network route.
            if record.status == "ready":
                record.status = "stored"
                self._persist()
            return {**record.public(False), **public_proxy(self.state_dir(account_id))}

    def is_busy(self, account_id: str) -> bool:
        return account_id in self._leases

    async def begin_login(self, email: str, region: str = "VN") -> dict[str, Any]:
        email = email.strip()
        if not email or "@" not in email:
            raise ValueError("Enter a valid Muse email address")
        async with self._lock:
            record = next((item for item in self._accounts.values()
                           if item.email.casefold() == email.casefold()), None)
            if record is None:
                record = AccountRecord(
                    id=uuid.uuid4().hex, email=email, label=email,
                )
                self._accounts[record.id] = record
            if record.id in self._leases:
                raise RuntimeError("Account is running a generation task")
            prior = self._pending_auth.pop(record.id, None)
            if prior is not None:
                await prior.close()
            auth = MuseAuth(state_dir=self.state_dir(record.id))
            self._pending_auth[record.id] = auth
            record.status = "sending_otp"
            record.last_error = None
            self._persist()

        try:
            await auth.restart_login()
            await auth.send_otp(email, region)
            record.status = "pending_otp"
            self._persist()
            return record.public()
        except Exception:
            record.status = "error"
            record.last_error = "Không gửi được OTP. Kiểm tra trạng thái / thử lại."
            self._persist()
            self._pending_auth.pop(record.id, None)
            await auth.close()
            raise

    async def confirm_login(self, account_id: str, otp: str) -> dict[str, Any]:
        async with self._lock:
            record = self.get(account_id)
            auth = self._pending_auth.get(account_id)
            if auth is None:
                raise ValueError("OTP session expired; send OTP again")
            if not otp.strip():
                raise ValueError("OTP must not be empty")
            await auth.confirm_otp(otp.strip())
            await auth.save_account()
            status = await auth.auth_check()
            if status.get("ok") is not True:
                raise RuntimeError("Muse session validation failed")
            await auth.save_cookies()
            record.status = "ready"
            record.last_verified_at = now()
            record.last_error = None
            self._persist()
            self._pending_auth.pop(account_id, None)
            await auth.close()
            return record.public(account_id in self._leases)

    async def verify(self, account_id: str) -> dict[str, Any]:
        record = self.get(account_id)
        if not (self.state_dir(account_id) / "cookies.json").exists():
            record.status = "needs_login"
            self._persist()
            return record.public(self.is_busy(account_id))
        auth = MuseAuth(state_dir=self.state_dir(account_id))
        try:
            await auth.load_cookies()
            check = await auth.auth_check()
            record.status = "ready" if check.get("ok") is True else "needs_login"
            if record.status == "ready":
                record.last_verified_at = now()
                record.last_error = None
            else:
                record.last_error = "Muse session không còn hợp lệ"
        except Exception:
            record.status = "error"
            record.last_error = "Không xác minh được phiên Muse"
        finally:
            await auth.close()
        self._persist()
        return record.public(self.is_busy(account_id))

    async def verify_all(self) -> None:
        await asyncio.gather(
            *(self.verify(record.id) for record in self._accounts.values()
              if record.enabled and record.id not in self._leases
              and record.status not in {"pending_otp", "sending_otp"}),
            return_exceptions=True,
        )

    async def reserve_random(self, count: int) -> list[AccountRecord]:
        if count < 1:
            raise ValueError("Task count must be positive")
        async with self._lock:
            candidates = [
                record for record in self._accounts.values()
                if record.enabled and record.status == "ready"
                and record.id not in self._leases
            ]
            if len(candidates) < count:
                raise ValueError(
                    f"Cần {count} tài khoản Muse sẵn sàng, "
                    f"hiện chỉ có {len(candidates)} tài khoản còn rảnh."
                )
            selected = random.sample(candidates, count)
            self._leases.update(record.id for record in selected)
            return selected

    async def release(self, account_id: str) -> None:
        async with self._lock:
            self._leases.discard(account_id)

    async def set_enabled(self, account_id: str, enabled: bool) -> dict[str, Any]:
        async with self._lock:
            record = self.get(account_id)
            if account_id in self._leases:
                raise RuntimeError("Account is running a task")
            record.enabled = enabled
            self._persist()
            return record.public()

    async def remove(self, account_id: str) -> None:
        async with self._lock:
            self.get(account_id)
            if account_id in self._leases:
                raise RuntimeError("Account is running a task")
            auth = self._pending_auth.pop(account_id, None)
            if auth is not None:
                await auth.close()
            self._accounts.pop(account_id)
            self._persist()
            shutil.rmtree(self.state_dir(account_id), ignore_errors=True)

    async def close(self) -> None:
        pending = list(self._pending_auth.values())
        self._pending_auth.clear()
        await asyncio.gather(*(item.close() for item in pending), return_exceptions=True)
