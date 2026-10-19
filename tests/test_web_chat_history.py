"""API and service tests for durable Muse chat history (requires web dependencies)."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from muse_ai.web.app import app, service
from muse_ai.web.chat_store import ChatStore


def test_sqlite_chat_api_roundtrip(tmp_path, monkeypatch):
    store = ChatStore(tmp_path / "chats.sqlite3")
    monkeypatch.setattr(service, "chat_store", store)
    monkeypatch.setattr(service.accounts, "get", lambda account_id:
                        SimpleNamespace(id=account_id, enabled=True, status="ready"))

    with TestClient(app) as client:
        save = client.put("/api/chat/threads/thread-1", json={
            "accountId": "account-1", "sessionId": "session-1",
            "name": "My Muse thread", "messages": [
                {"role": "user", "text": "xin chào"}
            ],
        })
        assert save.status_code == 200, save.text
        listed = client.get("/api/chat/threads", params={"account_id": "account-1"})
        assert listed.status_code == 200
        assert len(listed.json()["threads"]) == 1
        assert listed.json()["threads"][0]["sessionId"] == "session-1"

        tamper = client.put("/api/chat/threads/thread-1", json={
            "accountId": "another-account", "sessionId": "session-1",
            "messages": [],
        })
        assert tamper.status_code == 400

    assert ChatStore(store.path).get("thread-1")["messages"][0]["text"] == "xin chào"


@pytest.mark.asyncio
async def test_service_sync_restores_long_assistant_text(tmp_path, monkeypatch):
    store = ChatStore(tmp_path / "chats.sqlite3")
    store.put({"id": "thread-1", "accountId": "account-1",
               "sessionId": "session-1", "messages": [
                   {"role": "user", "text": "ai vậy"},
                   {"role": "assistant", "text": "Dạ bẩm"}
               ]})
    monkeypatch.setattr(service, "chat_store", store)
    monkeypatch.setattr(service.accounts, "get", lambda _:
                        SimpleNamespace(enabled=True, status="ready"))
    monkeypatch.setattr(service.accounts, "is_busy", lambda _: False)

    class FakeClient:
        async def history(self, *, session_id, limit=80):
            assert session_id == "session-1"
            return {"messages": [
                {"role": "user", "content": "ai vậy"},
                {"role": "assistant", "content": "Dạ bẩm, đây là câu trả lời đầy đủ."}
            ]}

    monkeypatch.setitem(service._chat_clients, "account-1", FakeClient())
    result = await service.sync_muse_thread("thread-1")
    assert result["remote_count"] == 2
    assert result["thread"]["messages"][1]["text"] == "Dạ bẩm, đây là câu trả lời đầy đủ."
    assert len(result["thread"]["messages"]) == 2


def test_http_chat_send_saves_reply_without_browser_callback(tmp_path, monkeypatch):
    store = ChatStore(tmp_path / "chats.sqlite3")
    store.put({"id": "thread-1", "accountId": "account-1",
               "sessionId": "session-1", "messages": []})
    monkeypatch.setattr(service, "chat_store", store)
    scheduled = []
    monkeypatch.setattr(service, "schedule_chat_reconcile", scheduled.append)

    async def reply(*, message, session_id, timeout, account_id):
        return {"ok": True, "account_id": account_id,
                "session_id": session_id, "text": "Muse trả lời rất dài"}

    monkeypatch.setattr(service, "send_chat_message", reply)
    with TestClient(app) as client:
        response = client.post("/api/chat/send", json={
            "message": "xin chào", "session_id": "session-1",
            "account_id": "account-1", "thread_id": "thread-1", "timeout": 30,
        })
        assert response.status_code == 200, response.text

    saved = ChatStore(store.path).get("thread-1")
    assert [msg["role"] for msg in saved["messages"]] == ["user", "assistant"]
    assert saved["messages"][-1]["text"] == "Muse trả lời rất dài"
    assert scheduled == ["thread-1"]

def test_chat_send_multiple_uploads_and_restore(tmp_path, monkeypatch):
    from muse_ai.web import app as web_app
    from muse_ai.web.app import app, service

    store = ChatStore(tmp_path / "multi-uploads.sqlite3")
    store.put({
        "id": "thread-multi", "accountId": "account-multi",
        "sessionId": "session-multi", "messages": [],
    })
    monkeypatch.setattr(service, "chat_store", store)
    monkeypatch.setattr(web_app, "WEB_STATE_DIR", tmp_path)
    monkeypatch.setattr(service, "schedule_chat_reconcile", lambda _: None)
    called = []

    async def fake_chat(*, message, session_id, timeout, account_id, attachments=None):
        called.extend(attachments or [])
        assert [entry["kind"] for entry in attachments] == ["image", "document"]
        return {
            "ok": True, "text": "Tôi đã nhận hai file.",
            "session_id": session_id, "reply_finished": True,
            "completion_source": "muse_final", "media_job_id": None,
        }

    monkeypatch.setattr(service, "send_chat_message", fake_chat)
    with TestClient(app) as client:
        response = client.post(
            "/api/chat/send-files",
            data={
                "message": "Xử lý cả hai file",
                "account_id": "account-multi",
                "session_id": "session-multi",
                "thread_id": "thread-multi",
                "timeout": "30",
            },
            files=[
                ("files", ("image.png", b"fake-image", "image/png")),
                ("files", ("brief.txt", b"hello Muse", "text/plain")),
            ],
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert len(data["uploaded_files"]) == 2
        assert len(called) == 2
        for entry in data["uploaded_files"]:
            file_response = client.get(entry["url"])
            assert file_response.status_code == 200
        saved = store.get("thread-multi")
        assert [msg["role"] for msg in saved["messages"]] == ["user", "assistant"]
        assert len(saved["messages"][0]["uploads"]) == 2
        assert saved["messages"][-1]["text"] == "Tôi đã nhận hai file."


def test_chat_upload_rejects_unsupported_extensions(tmp_path, monkeypatch):
    from muse_ai.web import app as web_app
    from muse_ai.web.app import app, service
    store = ChatStore(tmp_path / "uploads.sqlite3")
    store.put({"id": "thread-one", "accountId": "acc", "messages": []})
    monkeypatch.setattr(service, "chat_store", store)
    monkeypatch.setattr(web_app, "WEB_STATE_DIR", tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/chat/send-files",
            data={"message": "Scan", "account_id": "acc",
                  "thread_id": "thread-one"},
            files=[("files", ("evil.exe", b"bad", "application/octet-stream"))],
        )
        assert response.status_code == 400
        assert "không được hỗ trợ" in response.json()["detail"]
