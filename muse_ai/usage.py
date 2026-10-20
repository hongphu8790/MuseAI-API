"""Weekly plan usage, as shown in Muse's Settings > General > Usage.

The web app reads it through a Next.js server action (``fetchSubscriptionAction``)
posted to ``https://muse.ai/``. The action id is a build-time hash; it is
stable across deploys until Meta changes the action, and can be overridden
with ``MUSE_SUBSCRIPTION_ACTION`` when it does (find it in the web app's JS:
``createServerReference)("<id>", ..., "fetchSubscriptionAction")``). The
request must carry the page's deployment id (``x-deployment-id``), read from
the muse.ai HTML, or the edge answers 403.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import re
from typing import Any

from .auth import MuseAuth

FETCH_SUBSCRIPTION_ACTION = "4029ac5c4bd03136487d91ebde5de39c64b35229b4"
_DEPLOYMENT = re.compile(r"dpl_[A-Za-z0-9]+")


@dataclass(frozen=True, slots=True)
class MuseUsage:
    plan: str
    tier_code: str
    percent_used: float | None
    resets_at: datetime | None
    quota_status: str | None
    topup_label: str | None
    raw: dict[str, Any]

    def summary(self) -> str:
        used = "?" if self.percent_used is None else f"{self.percent_used:g}%"
        reset = self.resets_at.astimezone().strftime("%Y-%m-%d %H:%M %Z") if self.resets_at else "?"
        parts = [f"plan={self.plan} ({self.tier_code})", f"weekly used={used}", f"resets={reset}",
                 f"status={self.quota_status or '?'}"]
        if self.topup_label:
            parts.append(f"extra tokens: {self.topup_label}")
        return "usage: " + ", ".join(parts)


def parse_subscription_payload(text: str) -> dict[str, Any]:
    """The server action answers React Flight rows (``<id>:<json>``); the
    subscription is the row whose JSON object has a ``subscription`` key."""
    for line in text.splitlines():
        _, sep, body = line.partition(":")
        if not sep or not body.startswith("{"):
            continue
        try:
            value = json.loads(body)
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("subscription"), dict):
            return value["subscription"]
    raise ValueError("no subscription object in the server-action response")


def usage_from_subscription(sub: dict[str, Any]) -> MuseUsage:
    tier = sub.get("tier") or {}
    usage = sub.get("usage") or {}
    resets = usage.get("resetsAt")
    return MuseUsage(
        plan=str(tier.get("name") or tier.get("tierId") or "?"),
        tier_code=str(tier.get("tierCode") or "?"),
        percent_used=usage.get("percentUsed"),
        resets_at=datetime.fromtimestamp(resets, tz=timezone.utc) if isinstance(resets, (int, float)) else None,
        quota_status=usage.get("quotaStatus"),
        topup_label=sub.get("topupRowValueLabel"),
        raw=sub,
    )


async def fetch_usage(auth: MuseAuth, *, action_id: str | None = None) -> MuseUsage:
    action = action_id or os.environ.get("MUSE_SUBSCRIPTION_ACTION") or FETCH_SUBSCRIPTION_ACTION
    if not list(auth.browser.cookies.jar):
        await auth.load_cookies()
    page = await auth.browser.get(auth.BASE + "/", headers={"Accept": "text/html"})
    match = _DEPLOYMENT.search(page.text)
    headers = {
        "Accept": "text/x-component",
        "Content-Type": "text/plain;charset=UTF-8",
        "Origin": auth.BASE,
        "Referer": auth.BASE + "/",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Next-Action": action,
    }
    if match:
        headers["x-deployment-id"] = match.group(0)
    response = await auth.browser.post(auth.BASE + "/", data='[{"includeAgreement":true}]', headers=headers)
    if response.status_code >= 400:
        raise RuntimeError(
            f"usage request failed: HTTP {response.status_code} {response.text[:120]!r} "
            "(if the action id changed, set MUSE_SUBSCRIPTION_ACTION)"
        )
    return usage_from_subscription(parse_subscription_payload(response.text))
