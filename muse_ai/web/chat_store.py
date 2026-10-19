"""Durable, account-scoped browser chat history and Muse transcript reconciliation."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


def _text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_text_content(item) for item in value)
    if not isinstance(value, dict):
        return ""
    for key in ("text", "output_text", "response_text", "reply"):
        if isinstance(value.get(key), str):
            return value[key]
    for key in ("content", "parts", "items", "value"):
        if key in value:
            return _text_content(value[key])
    return ""


def parse_muse_history(raw: Any) -> list[dict[str, str]]:
    """Parse the ordered message transcript; never treat arbitrary metadata as text."""
    def find_messages(node: Any) -> list | None:
        if isinstance(node, list):
            if any(isinstance(item, dict) and
                   any(key in item for key in ("role", "author", "sender", "speaker"))
                   for item in node):
                return node
            for item in node:
                result = find_messages(item)
                if result is not None:
                    return result
        if isinstance(node, dict):
            for key in ("messages", "transcript", "history", "result", "data", "conversation"):
                if key in node:
                    result = find_messages(node[key])
                    if result is not None:
                        return result
        return None

    items = find_messages(raw)
    if items is None:
        return []
    output: list[dict[str, str]] = []
    by_id: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        role = next((item.get(key) for key in ("role", "author", "sender", "speaker")
                     if item.get(key) is not None), None)
        if isinstance(role, dict):
            role = role.get("role") or role.get("name") or role.get("type")
        role = str(role or "").lower()
        if role in ("assistant", "model", "ai", "bot", "agent"):
            role = "assistant"
        elif role in ("user", "human", "client"):
            role = "user"
        else:
            continue
        text = _text_content(item.get("content"))
        if not text:
            text = _text_content(item)
        if not text.strip():
            continue
        msg = {"role": role, "text": text}
        item_id = item.get("id") or item.get("message_id") or item.get("messageId")
        if isinstance(item_id, str) and item_id in by_id:
            output[by_id[item_id]] = msg  # last stream snapshot is authoritative
        else:
            if isinstance(item_id, str):
                by_id[item_id] = len(output)
            output.append(msg)
    return output


def merge_messages(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """Merge a Muse transcript without losing older local turns or attachments.

    Match repeated text once per occurrence; insert older unmatched turns
    near their surviving neighbors instead of moving them to the end.
    """
    remote: list[dict] = []
    matches: dict[int, int] = {}
    for newer in incoming:
        if not isinstance(newer, dict) or newer.get("role") not in ("user", "assistant"):
            continue
        new_text = str(newer.get("text") or "")
        candidate = None
        for idx, old in enumerate(existing):
            if not isinstance(old, dict) or idx in matches or old.get("role") != newer.get("role"):
                continue
            if str(old.get("text") or "") == new_text:
                candidate = idx
                break
        if candidate is None:
            for idx, old in enumerate(existing):
                if not isinstance(old, dict) or idx in matches or old.get("role") != newer.get("role"):
                    continue
                old_text = str(old.get("text") or "")
                if (
                    (newer.get("role") == "assistant" and (
                        (len(old_text) >= 5 and new_text.startswith(old_text))
                        or (len(new_text) >= 5 and old_text.startswith(new_text))
                    ))
                    or (newer.get("role") == "user" and len(old_text) >= 5
                        and new_text.startswith(old_text + "\n\nCác tài liệu"))
                ):
                    candidate = idx
                    break
        if candidate is not None:
            matches[candidate] = len(remote)
            old = existing[candidate]
            old_uploads = old.get("uploads") or []
            new_uploads = newer.get("uploads") or []
            # A later browser cache snapshot may only contain filenames, not
            # the server-issued file IDs; never lose the durable attachment links.
            keep_uploads = (
                old_uploads if sum(bool(u.get("id")) for u in old_uploads) >
                sum(bool(u.get("id")) for u in new_uploads) else new_uploads
            )
            remote.append({
                **old, **newer,
                "text": (
                    str(old.get("text") or "")
                    if newer.get("role") == "user"
                    and new_text.startswith(str(old.get("text") or "") + "\n\nCác tài liệu")
                    else max(str(old.get("text") or ""), new_text, key=len)
                ),
                "files": newer.get("files") or old.get("files") or [],
                "uploads": keep_uploads,
            })
        else:
            remote.append(dict(newer))

    inserts: list[list[dict]] = [[] for _ in range(len(remote) + 1)]
    anchors = sorted(matches)
    for idx, old in enumerate(existing):
        if not isinstance(old, dict) or idx in matches:
            continue
        previous = next((anchor for anchor in reversed(anchors) if anchor < idx), None)
        following = next((anchor for anchor in anchors if anchor > idx), None)
        if previous is not None:
            slot = matches[previous] + 1
        elif following is not None:
            slot = matches[following]
        else:
            slot = 0  # No shared anchors: keep historical turns before new ones.
        inserts[slot].append(dict(old))

    merged: list[dict] = []
    for index, item in enumerate(remote):
        merged.extend(inserts[index])
        merged.append(item)
    merged.extend(inserts[len(remote)])
    return merged


class ChatStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS chat_threads (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                session_id TEXT,
                name TEXT NOT NULL,
                messages_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS ix_chat_account ON chat_threads(account_id)")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        return {
            "id": row["id"], "accountId": row["account_id"],
            "sessionId": row["session_id"], "name": row["name"],
            "messages": json.loads(row["messages_json"]),
            "updatedAt": row["updated_at"],
        }

    def get(self, thread_id: str) -> dict | None:
        with self._connect() as db:
            return self._decode(db.execute("SELECT * FROM chat_threads WHERE id = ?",
                                           (thread_id,)).fetchone())

    def list(self, account_id: str | None = None) -> list[dict]:
        with self._connect() as db:
            if account_id:
                rows = db.execute("SELECT * FROM chat_threads WHERE account_id = ? ORDER BY updated_at DESC",
                                  (account_id,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM chat_threads ORDER BY updated_at DESC").fetchall()
            return [self._decode(row) for row in rows]

    def put(self, thread: dict, *, from_muse: bool = False) -> dict:
        thread_id = str(thread.get("id") or "")
        account_id = str(thread.get("accountId") or "")
        if not thread_id or len(thread_id) > 128 or not account_id or len(account_id) > 128:
            raise ValueError("Thread id and account id are required (maximum 128 characters)")
        session_id = thread.get("sessionId") or None
        if session_id is not None and (not isinstance(session_id, str) or len(session_id) > 256):
            raise ValueError("Invalid Muse session_id")
        messages = thread.get("messages", [])
        if not isinstance(messages, list):
            raise ValueError("messages must be a list")
        messages = [
            item for item in messages
            if isinstance(item, dict)
            and item.get("role") in ("user", "assistant")
            and isinstance(item.get("text"), str)
        ]
        if len(json.dumps(messages, ensure_ascii=False)) > 8_000_000:
            raise ValueError("Conversation exceeds 8 MB per save")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = self._decode(db.execute("SELECT * FROM chat_threads WHERE id = ?",
                                               (thread_id,)).fetchone())
            if previous and previous["accountId"] != account_id:
                raise ValueError("Conversation is assigned to a different Muse account")
            if previous and previous["sessionId"] and session_id and previous["sessionId"] != session_id:
                raise ValueError("Cannot change the Muse session of an existing conversation")
            if previous:
                messages = merge_messages(previous["messages"], messages)
            stored = {
                "id": thread_id, "accountId": account_id,
                "sessionId": session_id or (previous or {}).get("sessionId"),
                "name": str(thread.get("name") or (previous or {}).get("name") or "Cuộc hội thoại")[:300],
                "messages": messages,
                "updatedAt": int(time.time() * 1000),
            }
            db.execute("""INSERT INTO chat_threads
                (id, account_id, session_id, name, messages_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    session_id=excluded.session_id, name=excluded.name,
                    messages_json=excluded.messages_json, updated_at=excluded.updated_at""",
                (stored["id"], stored["accountId"], stored["sessionId"], stored["name"],
                 json.dumps(stored["messages"], ensure_ascii=False), stored["updatedAt"]))
            return stored
