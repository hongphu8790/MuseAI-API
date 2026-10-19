from fastapi.testclient import TestClient

from muse_ai.web.app import JobRecord, app, safe_filename, service


def test_safe_filename_blocks_parent_components():
    assert safe_filename("../../secret.mp4") == "secret.mp4"
    assert safe_filename(r"..\..\secret.mp4") == "secret.mp4"


def test_job_public_contains_download_urls():
    job = JobRecord(
        id="abc123",
        prompt="Create a video",
        downloads=["one.mp4", "two.mp4"],
    )
    data = job.public()
    assert data["download_urls"] == [
        "/api/generations/abc123/files/one.mp4",
        "/api/generations/abc123/files/two.mp4",
    ]


def test_web_health_endpoint():
    with TestClient(app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["service"] == "MuseAI-API Web"


def test_web_index_is_served():
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "MuseAI Studio" in response.text
        assert "Chat thường" in response.text
        assert "/chat.js" in response.text
        assert "/chat.css" in response.text

        assert client.get("/chat.js").status_code == 200
        assert client.get("/chat.css").status_code == 200
        assert 'src="/chat.js"' in response.text
        assert 'href="/chat.css"' in response.text
        assert 'href="/auth-modal.css"' in response.text
        assert 'id="authDialog"' in response.text
        assert 'id="authPill"' in response.text
        assert 'class="panel auth-panel"' not in response.text
        assert 'id="accountList"' in response.text
        assert 'id="otpAccountSelect"' in response.text
        assert 'id="chatAccount"' in response.text
        assert 'id="taskCount"' in response.text
        assert 'src="/accounts.js"' in response.text
        assert 'href="/accounts.css"' in response.text
        assert 'href="/jobs-compact.css"' in response.text
        assert '<details class="job-results hidden">' in response.text
        assert '<details class="job-diagnostics hidden">' in response.text
        assert client.get("/accounts.js").status_code == 200
        assert client.get("/accounts.css").status_code == 200


def test_chat_static_assets_are_served():
    with TestClient(app) as client:
        js = client.get("/chat.js")
        css = client.get("/chat.css")

    assert js.status_code == 200
    assert 'fetch("/api/chat/send"' in js.text
    assert css.status_code == 200
    assert ".chat-bubble" in css.text


def test_chat_send_rejects_empty_message():
    with TestClient(app) as client:
        response = client.post(
            "/api/chat/send",
            json={"message": "   "},
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "Message is required"


def test_chat_send_uses_session_id(monkeypatch):
    async def fake_auth_status():
        return {"authenticated": True, "outcome": "validated"}

    async def fake_send_chat_message(*, message, session_id, timeout, account_id):
        assert account_id is None
        assert message == "Xin chào Muse"
        assert session_id == "session-existing"
        assert timeout == 45
        return {
            "ok": True,
            "session_id": "session-existing",
            "text": "Xin chào!",
        }

    monkeypatch.setattr(service, "auth_status", fake_auth_status)
    monkeypatch.setattr(
        service,
        "send_chat_message",
        fake_send_chat_message,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/chat/send",
            json={
                "message": "Xin chào Muse",
                "session_id": "session-existing",
                "timeout": 45,
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "session_id": "session-existing",
        "text": "Xin chào!",
    }
