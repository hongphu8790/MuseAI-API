from __future__ import annotations

import json
from typing import Any

ASSISTANT_ROLES = {"assistant", "model", "ai", "bot", "agent"}
TEXT_KEYS = ("text", "output_text", "reply", "response_text")
ROLE_KEYS = ("role", "author", "sender", "speaker")


def _role_of(node: dict[str, Any]) -> str | None:
    for key in ROLE_KEYS:
        value = node.get(key)
        if isinstance(value, str):
            return value.strip().lower()
        if isinstance(value, dict):
            for nested_key in ("role", "name", "type"):
                nested = value.get(nested_key)
                if isinstance(nested, str):
                    return nested.strip().lower()
    return None


def _append(out: list[str], value: str) -> None:
    text = value.strip()
    if not text:
        return
    if text not in out:
        out.append(text)


def extract_text_fragments(
    value: Any,
    *,
    assistant_only: bool = False,
) -> list[str]:
    """Extract human-readable text from Muse stream/history structures.

    Muse responses have changed shape across web-client revisions. This walker
    intentionally supports the common forms seen in chat streams/history:
    role+content messages, typed text blocks, delta objects, and JSON strings.
    """

    out: list[str] = []

    def walk(node: Any, inherited_assistant: bool = False) -> None:
        if node is None:
            return

        if isinstance(node, str):
            stripped = node.strip()
            if stripped.startswith(("{", "[")):
                try:
                    walk(json.loads(stripped), inherited_assistant)
                    return
                except (TypeError, ValueError):
                    pass
            if inherited_assistant or not assistant_only:
                _append(out, node)
            return

        if isinstance(node, list):
            for item in node:
                walk(item, inherited_assistant)
            return

        if not isinstance(node, dict):
            return

        role = _role_of(node)
        is_assistant = inherited_assistant or role in ASSISTANT_ROLES
        allow_text = is_assistant or not assistant_only
        consumed: set[str] = set()

        if allow_text:
            for key in TEXT_KEYS:
                raw = node.get(key)
                if isinstance(raw, str):
                    _append(out, raw)
                    consumed.add(key)

        # Common chat message content shapes:
        # {"role":"assistant","content":"..."}
        # {"role":"assistant","content":[{"type":"text","text":"..."}]}
        if "content" in node:
            content = node.get("content")
            if isinstance(content, str):
                if allow_text:
                    _append(out, content)
            else:
                walk(content, is_assistant)
            consumed.add("content")

        # Delta/message wrappers can contain nested text.
        for key in ("delta", "message", "output", "result", "data", "item"):
            if key in node:
                walk(node.get(key), is_assistant)
                consumed.add(key)

        # Typed text blocks without an explicit role.
        kind = node.get("type") or node.get("kind")
        if isinstance(kind, str) and kind.lower() in {
            "text",
            "output_text",
            "assistant_text",
            "message_text",
        }:
            raw = node.get("text")
            if isinstance(raw, str) and allow_text:
                _append(out, raw)
                consumed.add("text")

        # Recurse through nested structures, but don't scan arbitrary scalar
        # metadata such as IDs, URLs, MIME types, labels, etc.
        for key, child in node.items():
            if key in consumed or key in ROLE_KEYS or key in TEXT_KEYS:
                continue
            if isinstance(child, (dict, list)):
                walk(child, is_assistant)

    walk(value)
    return out


def extract_assistant_texts(value: Any) -> list[str]:
    return extract_text_fragments(value, assistant_only=True)


def assistant_response_finished(value: Any) -> bool:
    """Only trust an explicit terminal flag on the most recent assistant message.

    Absence of a terminal flag is *unknown*, not completion. This supports
    multiple Muse payload shapes without treating a quiet stream as finished.
    """
    def messages(node: Any) -> list | None:
        if isinstance(node, list):
            if any(isinstance(item, dict) and _role_of(item) in ASSISTANT_ROLES
                   for item in node):
                return node
            for item in node:
                found = messages(item)
                if found is not None:
                    return found
        elif isinstance(node, dict):
            for key in ("messages", "result", "data", "history", "transcript"):
                if key in node:
                    found = messages(node[key])
                    if found is not None:
                        return found
        return None

    transcript = messages(value) or []
    assistants = [msg for msg in transcript if isinstance(msg, dict)
                  and _role_of(msg) in ASSISTANT_ROLES]
    if not assistants:
        return False
    latest = assistants[-1]
    status = latest.get("status") or latest.get("state")
    reason = latest.get("finish_reason") or latest.get("stop_reason")
    return (
        status in ("complete", "completed", "done", "finished")
        or reason in ("stop", "end_turn", "complete", "completed")
        or latest.get("is_final") is True
        or latest.get("is_complete") is True
        or latest.get("finished_at") is not None
    )


def muse_video_task_pending(value: Any) -> bool:
    """Check explicit task/job state, not an assistant's natural-language claims.

    A finalized chat message isn't necessarily the end of a background render.
    Only media/tool task records can keep the video watcher running.
    """
    pending = {"queued", "pending", "running", "processing", "generating",
               "rendering", "in_progress", "started", "submitted", "waiting"}
    task_keys = {"jobs", "tasks", "generations", "tool_calls", "media_jobs",
                 "render_jobs", "video_tasks"}
    status_keys = ("status", "state", "phase")

    def walk(node: Any, in_task: bool = False) -> bool:
        if isinstance(node, list):
            return any(walk(item, in_task) for item in node)
        if not isinstance(node, dict):
            return False
        kind = str(node.get("kind") or node.get("type") or "").lower()
        task_node = in_task or kind in {
            "video_generation", "generate_video", "video_task",
            "media_generation", "video_render",
        }
        if task_node and any(
            isinstance(node.get(k), str) and node[k].lower() in pending
            for k in status_keys
        ):
            return True
        return any(
            walk(child, task_node or key in task_keys)
            for key, child in node.items() if isinstance(child, (dict, list))
        )

    return walk(value)


def muse_video_task_terminal(value: Any) -> bool:
    """True only when Muse reports an explicit terminal video-task state.

    A finalized assistant chat or a missing task list is not proof the
    asynchronous render has ended.
    """
    terminal = {"failed", "error", "rejected", "cancelled", "canceled",
                "completed", "complete", "finished", "done"}
    task_keys = {"jobs", "tasks", "generations", "tool_calls", "media_jobs",
                 "render_jobs", "video_tasks"}

    def walk(node: Any, in_task: bool = False) -> bool:
        if isinstance(node, list):
            return any(walk(item, in_task) for item in node)
        if not isinstance(node, dict):
            return False
        kind = str(node.get("kind") or node.get("type") or "").lower()
        task_node = in_task or kind in {
            "video_generation", "generate_video", "video_task",
            "media_generation", "video_render",
        }
        if task_node and any(
            isinstance(node.get(k), str) and node[k].lower() in terminal
            for k in ("status", "state", "phase")
        ):
            return True
        return any(walk(child, task_node or key in task_keys)
                   for key, child in node.items() if isinstance(child, (dict, list)))

    return walk(value)


def terminal_video_task_ids(value: Any) -> set[str]:
    """Identity + terminal status, only for explicitly identifiable video jobs.

    Prevents an old completed video task elsewhere in the thread from
    falsely terminating a new submission.
    """
    terminal = {"failed", "error", "rejected", "cancelled", "canceled",
                "completed", "complete", "finished", "done"}
    task_keys = {"jobs", "tasks", "generations", "tool_calls", "media_jobs",
                 "render_jobs", "video_tasks"}
    result: set[str] = set()

    def walk(node: Any, in_task: bool = False) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, in_task)
        elif isinstance(node, dict):
            kind = str(node.get("kind") or node.get("type") or "").lower()
            task_node = in_task or kind in {
                "video_generation", "generate_video", "video_task",
                "media_generation", "video_render",
            }
            status = str(node.get("status") or node.get("state") or node.get("phase") or "").lower()
            if task_node and status in terminal:
                ident = next((node.get(key) for key in (
                    "task_id", "job_id", "generation_id", "id",
                ) if isinstance(node.get(key), (str, int))), None)
                if ident is not None:
                    result.add(str(ident) + ":" + status)
            for key, child in node.items():
                if isinstance(child, (dict, list)):
                    walk(child, task_node or key in task_keys)

    walk(value)
    return result


def latest_assistant_text(value: Any) -> str:
    """Last visible assistant text from Muse history or stream."""
    fragments = extract_assistant_texts(value)
    return fragments[-1] if fragments else ""


def coalesce_text_fragments(fragments: list[str]) -> str:
    """Combine stream fragments while handling cumulative delta snapshots."""

    clean = [fragment.strip() for fragment in fragments if fragment.strip()]
    if not clean:
        return ""

    # If each newer fragment includes the previous fragment, the stream is
    # sending cumulative snapshots. Return the longest/final snapshot.
    monotonic = True
    previous = clean[0]
    for current in clean[1:]:
        if not current.startswith(previous):
            monotonic = False
            break
        previous = current
    if monotonic:
        return clean[-1]

    # Otherwise treat them as incremental chunks. Avoid duplicate neighboring
    # chunks and preserve punctuation/spacing exactly as supplied.
    parts: list[str] = []
    for fragment in clean:
        if parts and fragment == parts[-1]:
            continue
        parts.append(fragment)

    # Full messages from history are usually separate strings. If any fragment
    # is much larger than the combined small deltas, prefer the longest one.
    longest = max(parts, key=len)
    if len(longest) >= 32 and all(
        piece == longest or piece in longest for piece in parts
    ):
        return longest

    return "".join(parts)
