from __future__ import annotations

import html as html_lib
import json
import re
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx
from curl_cffi.requests import AsyncSession as BrowserAsyncSession

from .errors import MuseAuthError


@dataclass(slots=True)
class MuseBootstrap:
    gateway_url: str
    vm_name: str
    admission: str
    notary: str | None = None
    locale: str = "en-US"

    @property
    def vm_id(self) -> str:
        if self.vm_name:
            return self.vm_name
        host = urlparse(self.gateway_url).hostname or ""
        return host.split(".", 1)[0]

    def websocket_url(self) -> str:
        params = {
            "vm_id": self.vm_id,
            "auth_" + "token": self.admission,
            "app_id": "hatch-web",
            "request_id": str(uuid.uuid4()),
        }
        if self.notary:
            params["notary_" + "token"] = self.notary
        return "wss://hatch.metaaivm.com/v1/noise?" + urlencode(params)


class MuseAuth:
    BASE = "https://muse.ai"

    def __init__(self, *, state_dir: str | Path = ".muse-state", timeout: float = 30.0) -> None:
        self.state_dir = Path(state_dir)
        self.cookie_path = self.state_dir / "cookies.json"
        self.waterfall_id: str | None = None
        self.csrf_token: str | None = None
        self.login_referer = self.BASE + "/"
        self.browser = BrowserAsyncSession(
            impersonate="chrome",
            timeout=timeout,
            headers={
                "Accept-Language": "vi-VN,vi;q=0.9,fr-FR;q=0.8,fr;q=0.7,en-US;q=0.6,en;q=0.5",
            },
        )
        self.http = httpx.AsyncClient(
            base_url=self.BASE,
            follow_redirects=True,
            timeout=timeout,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/153.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "vi-VN,vi;q=0.9,fr-FR;q=0.8,fr;q=0.7,en-US;q=0.6,en;q=0.5",
                "Sec-CH-UA": '"Google Chrome";v="153", "Not_A Brand";v="8", "Chromium";v="153"',
                "Sec-CH-UA-Mobile": "?0",
                "Sec-CH-UA-Platform": '"Windows"',
            },
        )

    def _browser_cookie_names(self) -> list[str]:
        return sorted({cookie.name for cookie in self.browser.cookies.jar})

    def _sync_browser_to_httpx(self) -> None:
        for cookie in self.browser.cookies.jar:
            self.http.cookies.set(
                cookie.name,
                cookie.value,
                domain=cookie.domain,
                path=cookie.path or "/",
            )

    async def prime_browser_session(self) -> None:
        # The first AYM/H navigation is fingerprint-sensitive.  Use curl_cffi's
        # Chrome impersonation for the complete cross-site redirect cycle.
        response = await self.browser.get(
            self.BASE + "/",
            allow_redirects=True,
        )
        if response.status_code >= 400:
            raise MuseAuthError(
                "Muse browser bootstrap failed: "
                f"HTTP {response.status_code} at {response.url}: {response.text[:500]}"
            )
        self.login_referer = str(response.url)
        if "aymh_complete=1" not in self.login_referer:
            raise MuseAuthError(
                "Muse AYM/H redirect cycle did not complete; "
                f"final_url={self.login_referer!r}, cookies={self._browser_cookie_names()}"
            )
        if "datr" not in self._browser_cookie_names():
            raise MuseAuthError(
                "Muse browser bootstrap completed without the datr cookie; "
                "the AYM/H flow likely changed."
            )
        self._sync_browser_to_httpx()
        try:
            await self.browser.get(
                self.BASE + "/api/consent/status",
                headers={"Accept": "*/*", "Referer": self.login_referer},
            )
        except Exception:
            pass
        self._sync_browser_to_httpx()

    def _flow_headers(self, *, include_csrf: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "*/*",
            "Origin": self.BASE,
            "Referer": self.login_referer,
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }
        if self.waterfall_id:
            headers["x-hatch-waterfall-id"] = self.waterfall_id
        if include_csrf:
            if not self.csrf_token:
                raise MuseAuthError("native auth CSRF token is missing; restart login first")
            headers["x-hatch-csrf-token"] = self.csrf_token
            headers["x-hatch-caa-reg-entry-point"] = "login_home"
        return headers

    async def restart_login(self) -> dict:
        self.waterfall_id = str(uuid.uuid4())
        self.csrf_token = None
        await self.prime_browser_session()
        response = await self.browser.post(
            self.BASE + "/api/auth/native/restart",
            json={},
            headers=self._flow_headers(),
        )
        if response.status_code >= 400:
            cookie_names = self._browser_cookie_names()
            raise MuseAuthError(
                "Muse native auth restart failed: "
                f"HTTP {response.status_code}: {response.text[:500]} "
                f"(referer={self.login_referer!r}, cookies={cookie_names})"
            )
        data = response.json()
        token = data.get("csrf_token")
        if not isinstance(token, str) or not token:
            raise MuseAuthError("Muse native auth restart did not return csrf_token")
        self.csrf_token = token
        self._sync_browser_to_httpx()
        # The web app's subsequent send-otp / confirm-otp requests use the root
        # document as Referer rather than the aymh_complete navigation URL.
        self.login_referer = self.BASE + "/"
        return data

    async def send_otp(self, contact_point: str, region: str = "VN") -> dict:
        response = await self.browser.post(
            self.BASE + "/api/auth/native/send-otp",
            json={"contact_point": contact_point, "region": region},
            headers=self._flow_headers(include_csrf=True),
        )
        if response.status_code >= 400:
            raise MuseAuthError(
                f"Muse send OTP failed: HTTP {response.status_code}: {response.text[:500]}"
            )
        self._sync_browser_to_httpx()
        return response.json()

    async def confirm_otp(self, otp_code: str) -> dict:
        response = await self.browser.post(
            self.BASE + "/api/auth/native/confirm-otp",
            json={"otp_code": otp_code},
            headers=self._flow_headers(include_csrf=True),
        )
        if response.status_code >= 400:
            raise MuseAuthError(
                f"Muse OTP failed: HTTP {response.status_code}: {response.text[:500]}"
            )
        self._sync_browser_to_httpx()
        return response.json()

    async def save_account(self) -> dict:
        response = await self.browser.post(
            self.BASE + "/api/auth/save-account",
            headers={
                "Accept": "*/*",
                "Origin": self.BASE,
                "Referer": self.BASE + "/",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
            },
        )
        self._sync_browser_to_httpx()
        if response.status_code >= 400:
            raise MuseAuthError(
                f"Muse save-account failed: HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )
        try:
            return response.json()
        except ValueError:
            return {"saved": response.status_code < 400}

    async def login_interactive(
        self,
        contact_point: str,
        *,
        region: str = "VN",
        otp_code: str | None = None,
    ) -> dict:
        await self.restart_login()
        await self.send_otp(contact_point, region)
        code = otp_code or input("Muse OTP: ").strip()
        result = await self.confirm_otp(code)
        await self.save_account()
        check = await self.auth_check()
        if check.get("ok") is not True:
            raise MuseAuthError(
                "Muse login completed but account validation failed: "
                f"{check}"
            )
        await self.save_cookies()
        return result

    async def auth_check(self) -> dict:
        response = await self.browser.post(
            self.BASE + "/api/auth/check",
            headers={
                "Accept": "*/*",
                "Origin": self.BASE,
                "Referer": self.BASE + "/thread/new",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
            },
        )
        self._sync_browser_to_httpx()
        if response.status_code >= 400:
            return {
                "ok": False,
                "status": response.status_code,
                "detail": response.text[:500],
            }
        try:
            data = response.json()
        except ValueError:
            return {
                "ok": False,
                "status": response.status_code,
                "detail": "non-JSON response",
            }
        if "ok" not in data:
            data["ok"] = response.status_code < 400 and bool(
                data.get("viewer_id") or data.get("access_token")
            )
        return data

    async def ensure_session(
        self,
        *,
        contact_point: str | None = None,
        region: str = "VN",
        otp_code: str | None = None,
    ) -> None:
        if self.cookie_path.exists():
            await self.load_cookies()
            check = await self.auth_check()
            if check.get("ok") is True:
                return

            # Older versions of this client saved hatch_sess immediately after OTP
            # but skipped the browser's /api/auth/save-account commit.  Try to repair
            # that saved session before forcing a new OTP flow.
            try:
                saved = await self.save_account()
                if saved.get("saved") is True:
                    check = await self.auth_check()
                    if check.get("ok") is True:
                        await self.save_cookies()
                        return
            except Exception:
                pass
        if not contact_point:
            raise MuseAuthError(
                "No valid saved Muse session. Run: muse-ai login --email <email>"
            )
        await self.login_interactive(contact_point, region=region, otp_code=otp_code)
        check = await self.auth_check()
        if check.get("ok") is not True:
            raise MuseAuthError("Muse login completed but the session did not validate.")

    @staticmethod
    def _find_string(text: str, field: str, required: bool = True) -> str | None:
        backslash = chr(92)
        normalized = html_lib.unescape(text).replace(backslash + '"', '"').replace(backslash + "/", "/")
        match = re.search(
            rf'"{re.escape(field)}"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"',
            normalized,
        )
        if not match:
            if required:
                raise MuseAuthError(f"bootstrap field {field!r} not found")
            return None
        raw = match.group(1)
        try:
            return json.loads('"' + raw + '"')
        except Exception:
            return raw

    def _muse_api_headers(self, *, referer: str | None = None) -> dict[str, str]:
        return {
            "Accept": "*/*",
            "Origin": self.BASE,
            "Referer": referer or self.BASE + "/",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }

    async def bootstrap_page(self) -> MuseBootstrap:
        response = await self.browser.get(
            self.BASE + "/",
            headers={
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
            },
            allow_redirects=True,
        )
        if response.status_code >= 400:
            raise MuseAuthError(
                "Muse bootstrap page failed: "
                f"HTTP {response.status_code} at {response.url}: {response.text[:500]}"
            )
        self._sync_browser_to_httpx()
        text = response.text
        gateway_field = "gateway" + "Url"
        vm_field = "vm" + "Name"
        admission_field = "auth" + "Token"
        notary_field = "notary" + "Token"
        locale_field = "viewer" + "Locale"
        return MuseBootstrap(
            gateway_url=self._find_string(text, gateway_field) or "",
            vm_name=self._find_string(text, vm_field) or "",
            admission=self._find_string(text, admission_field) or "",
            notary=self._find_string(text, notary_field, required=False),
            locale=(
                self._find_string(text, locale_field, required=False) or "en-US"
            ).replace("_", "-"),
        )

    async def wake_vm(self, bootstrap: MuseBootstrap, retry_count: int = 0) -> dict:
        response = await self.browser.post(
            self.BASE + "/api/hatch/vm/wake",
            json={
                "vm_id": bootstrap.vm_id,
                "retry_count": retry_count,
                "connect_attempt_id": str(uuid.uuid4()),
            },
            headers=self._muse_api_headers(),
        )
        self._sync_browser_to_httpx()
        if response.status_code >= 400:
            raise MuseAuthError(
                "Muse VM wake failed: "
                f"HTTP {response.status_code}: {response.text[:500]} "
                f"(cookies={self._browser_cookie_names()})"
            )
        return response.json()

    async def refresh_hatch_token(self, bootstrap: MuseBootstrap) -> MuseBootstrap:
        response = await self.browser.post(
            self.BASE + "/api/hatch/token",
            json={
                "vmAddress": bootstrap.gateway_url,
                "vmName": bootstrap.vm_name,
            },
            headers=self._muse_api_headers(),
        )
        self._sync_browser_to_httpx()
        if response.status_code >= 400:
            raise MuseAuthError(
                "Muse Hatch token request failed: "
                f"HTTP {response.status_code}: {response.text[:500]}"
            )
        data = response.json()
        admission = data.get("token") or bootstrap.admission
        notary = data.get("notary_" + "token") or bootstrap.notary
        if not admission:
            raise MuseAuthError("Hatch admission credential is missing")
        return replace(bootstrap, admission=admission, notary=notary)

    async def bootstrap_hatch(self, *, wake: bool = True) -> MuseBootstrap:
        bootstrap = await self.bootstrap_page()
        if wake:
            await self.wake_vm(bootstrap)
        return await self.refresh_hatch_token(bootstrap)

    async def save_cookies(self, path: str | Path | None = None) -> None:
        target = Path(path) if path is not None else self.cookie_path
        target.parent.mkdir(parents=True, exist_ok=True)
        merged: dict[tuple[str, str, str], dict] = {}
        for jar in (self.browser.cookies.jar, self.http.cookies.jar):
            for cookie in jar:
                key = (cookie.name, cookie.domain or "", cookie.path or "/")
                merged[key] = {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path or "/",
                }
        target.write_text(
            json.dumps(list(merged.values()), indent=2), encoding="utf-8"
        )

    async def load_cookies(self, path: str | Path | None = None) -> None:
        target = Path(path) if path is not None else self.cookie_path
        data = json.loads(target.read_text(encoding="utf-8"))
        for c in data:
            domain = c.get("domain")
            cookie_path = c.get("path", "/")
            self.http.cookies.set(
                c["name"], c["value"], domain=domain, path=cookie_path
            )
            self.browser.cookies.set(
                c["name"], c["value"], domain=domain, path=cookie_path
            )

    async def close(self) -> None:
        await self.browser.close()
        await self.http.aclose()
