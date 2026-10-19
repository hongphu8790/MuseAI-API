import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from muse_ai.web.app import app, service
from muse_ai.web.chat_media import ChatMediaJob, ChatMediaManager, media_references


def test_file_attachment_parser_and_safe_file_route(tmp_path):
    payload = {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "file_ref", "path": "sandbox://workspace/report.pdf",
                     "mime_type": "application/pdf"}
                ]
            }
        ]
    }
    refs = media_references(payload)
    assert len(refs) == 1
    kind, file_ref = refs[0]
    assert kind == "file"
    assert file_ref.path == "sandbox://workspace/report.pdf"


def test_media_parser_finds_image_and_video():
    payload = {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "kind": "image",
                        "data": {
                            "images": [
                                {
                                    "path": "sandbox://workspace/one.png",
                                    "mime_type": "image/png",
                                }
                            ]
                        },
                    },
                    {
                        "kind": "video",
                        "path": "sandbox://workspace/movie.mp4",
                        "mime_type": "video/mp4",
                    },
                ],
            }
        ]
    }
    kinds = [kind for kind, _ in media_references(payload)]
    assert kinds.count("image") == 1
    assert kinds.count("video") == 1


@pytest.mark.asyncio
async def test_chat_media_monitor_downloads_and_serves_file(tmp_path, monkeypatch):
    manager = ChatMediaManager(tmp_path / "chat-media")
    payload = {
        "messages": [
            {"role": "assistant", "content": [
                {"type": "image", "data": {"images": [
                    {"path": "sandbox://workspace/generated.png", "mime_type": "image/png"}
                ]}}
            ]}
        ]
    }

    async def fetch(_session):
        return payload

    async def download(kind, ref, target):
        assert kind == "image"
        target.write_bytes(b"fake-png")
        return target

    job = manager.start(
        session_id="session-1",
        baseline=set(),
        fetch=fetch,
        download=download,
        timeout=0.3,
        poll_interval=0.01,
        settle_seconds=0.01,
    )
    await manager.tasks[job.id]
    assert job.status == "completed"
    assert len(job.files) == 1
    assert job.files[0]["url"].startswith("/api/chat/media/")

    monkeypatch.setattr(service, "chat_media", manager)
    with TestClient(app) as client:
        state = client.get(f"/api/chat/media/{job.id}")
        assert state.status_code == 200
        filename = job.files[0]["filename"]
        preview = client.get(f"/api/chat/media/{job.id}/files/{filename}")
        assert preview.status_code == 200
        assert preview.content == b"fake-png"
        assert "inline" in preview.headers["content-disposition"]
        download_result = client.get(
            f"/api/chat/media/{job.id}/files/{filename}?download=1"
        )
        assert download_result.status_code == 200
        assert "attachment" in download_result.headers["content-disposition"]
        wrong = client.get(f"/api/chat/media/{job.id}/files/not-found.png")
        assert wrong.status_code == 404


@pytest.mark.asyncio
async def test_monitor_ignores_previous_media(tmp_path):
    manager = ChatMediaManager(tmp_path / "chat-media")
    payload = {"items": [{"kind": "image", "path": "sandbox://workspace/old.png"}]}

    async def fetch(_session):
        return payload

    async def download(*_args):
        raise AssertionError("Old media must not be downloaded")

    job = manager.start(
        session_id="session-old",
        baseline={"image:sandbox://workspace/old.png"},
        fetch=fetch,
        download=download,
        timeout=0.035,
        poll_interval=0.01,
        settle_seconds=0.01,
    )
    await manager.tasks[job.id]
    assert job.status == "reply_unconfirmed"
    assert job.files == []

@pytest.mark.asyncio
async def test_monitor_reports_finished_reply_without_video(tmp_path):
    manager = ChatMediaManager(tmp_path / "chat-media")
    reply = "Tôi không thể tạo video ở khu vực này."

    async def fetch(_session):
        return {"messages": [
            {"role": "assistant", "content": reply, "finish_reason": "stop"}
        ], "tasks": [{"id": "task-new", "kind": "video_generation", "status": "failed"}]}

    async def download(*_args):
        raise AssertionError("There is no video to download")

    job = manager.start(
        session_id="session-final",
        baseline=set(),
        fetch=fetch,
        download=download,
        response_text=reply,
        expected_kind="video",
        timeout=1,
        poll_interval=0.001,
    )
    await manager.tasks[job.id]
    assert job.status == "no_media"
    assert job.error_code == "muse_finished_without_video"
    assert job.muse_message == reply
    assert reply in job.errors[0]


@pytest.mark.asyncio
async def test_monitor_does_not_fail_while_video_task_running(tmp_path):
    manager = ChatMediaManager(tmp_path / "chat-media")
    reply = "Đang tạo video."
    checks = 0

    async def fetch(_session):
        nonlocal checks
        checks += 1
        response = {"messages": [
            {"role": "assistant", "content": reply, "finish_reason": "stop"}
        ], "tasks": [{"kind": "video_generation",
                      "status": "running" if checks <= 2 else "completed"}]}
        if checks >= 3:
            response["media"] = {"kind": "video", "path": "workspace/final.mp4"}
        return response

    async def download(_kind, _ref, target):
        target.write_bytes(b"fake-video")
        return target

    job = manager.start(
        session_id="session-pending", baseline=set(),
        fetch=fetch, download=download, response_text=reply,
        expected_kind="video", timeout=1, poll_interval=0.001,
        settle_seconds=0.001,
    )
    await manager.tasks[job.id]
    assert checks >= 3
    assert job.status == "completed"
    assert len(job.files) == 1
    assert job.error_code is None
