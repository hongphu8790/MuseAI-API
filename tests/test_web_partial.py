from pathlib import Path

import pytest

from muse_ai.client import GenerationResult
from muse_ai.media import VideoRef
from muse_ai.web.account_pool import AccountPool, AccountRecord
from muse_ai.web.app import JobRecord, JobStore, service


@pytest.mark.asyncio
async def test_completed_video_with_failed_extra_media_is_partial_success(
    tmp_path, monkeypatch
):
    pool = AccountPool(tmp_path / "accounts")
    record = AccountRecord(
        id="account-one",
        email="test@example.com",
        label="Muse Test",
        status="ready",
    )
    pool._accounts[record.id] = record
    store = JobStore(tmp_path / "jobs")
    job = JobRecord(
        id="job-one",
        prompt="Generate a product video",
        account_id=record.id,
        account_label=record.label,
    )
    await store.add(job)

    class FakeClient:
        def __init__(self, auth, *, state_dir):
            pass

        async def connect(self):
            pass

        async def generate_video(self, *, output_dir, **kwargs):
            out = Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)
            video = out / "valid.mp4"
            video.write_bytes(b"valid-fake-video")
            return GenerationResult(
                session_id="thread-1",
                videos=[VideoRef(path="workspace/valid.mp4")],
                downloaded=[video],
                download_errors=[
                    "video[2] workspace/missing.mp4: "
                    "Hatch RPC returned HTTP 404: path not found"
                ],
            )

        async def close(self):
            pass

    class FakeAuth:
        def __init__(self, *, state_dir):
            pass

    monkeypatch.setattr(service, "accounts", pool)
    monkeypatch.setattr(service, "jobs", store)
    monkeypatch.setattr("muse_ai.web.app.MuseClient", FakeClient)
    monkeypatch.setattr("muse_ai.web.app.MuseAuth", FakeAuth)

    await service._run_generation(job.id, [])

    actual = store.get(job.id)
    assert actual.status == "completed_partial"
    assert actual.downloads == ["valid.mp4"]
    assert len(actual.download_errors) == 1
    assert actual.public()["download_urls"] == [
        "/api/generations/job-one/files/valid.mp4"
    ]
    assert "Đã tải 1 video" in actual.message
