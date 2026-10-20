from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Route:
    http_method: str
    path: str
    service: str = "daemon"
    subscription: bool = False
    binary: bool = False


ROUTES: dict[str, Route] = {
    "connection.ping": Route("POST", "/api/ping"),
    "model.get": Route("GET", "/model"),
    "client.register_capabilities": Route("POST", "/client/register-capabilities"),
    "chat.stream": Route("POST", "/chat/stream", subscription=True),
    "chat.stream_events": Route("POST", "/chat/stream-events", subscription=True),
    "chat.subscribe": Route("POST", "/chat/subscribe", subscription=True),
    "chat.history": Route("GET", "/chat/history"),
    "chat.history_window": Route("GET", "/chat/history-window"),
    "sessions.list": Route("GET", "/api/session/list"),
    "fs.stats": Route("POST", "/fs/stats"),
    "fs.list": Route("POST", "/fs/list"),
    "fs.stat": Route("POST", "/fs/stat"),
    "fs.read": Route("POST", "/fs/read"),
    "fs.reads": Route("POST", "/fs/reads"),
    "fs.write": Route("POST", "/fs/write"),
    "fs.delete": Route("POST", "/fs/delete"),
    "fs.rename": Route("POST", "/fs/rename"),
    "fs.mkdir": Route("POST", "/fs/mkdir"),
    "fs.library": Route("POST", "/fs/library"),
    "fs.export": Route("POST", "/fs/export", binary=True),
    "fs.subscribe": Route("POST", "/api/fs/subscribe", subscription=True),
}


CHAT_CAPABILITIES = [
    "chat_cancel",
    "delta_stream",
    "custom_reactions",
    "custom_reactions_facebook_thumbs_up_v1",
]


def replay_subscribe_params(
    after_stream_seq: int = 0,
    after_chat_event_seq: int | None = None,
) -> dict:
    if after_chat_event_seq is None:
        after_chat_event_seq = after_stream_seq
    return {
        "after_stream_seq": max(0, int(after_stream_seq)),
        "after_chat_event_seq": max(0, int(after_chat_event_seq)),
        "capabilities": list(CHAT_CAPABILITIES),
    }
