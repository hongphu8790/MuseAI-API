from __future__ import annotations

import base64
from pathlib import Path

import pytest
from PIL import Image

from muse_ai.client import MuseClient

from muse_ai.attachments import build_image_item, build_items
from muse_ai.filesystem import normalize_gateway_path
from muse_ai.media import VideoRef, extract_image_refs, extract_session_ids, extract_video_refs
from muse_ai.noise import CipherState, NoiseXXInitiator, PROTOCOL, SymmetricState, hkdf_noise, nonce12
from muse_ai.wire import NoiseChunk, NoiseReassembler, split_noise_payload


def test_cipher_state_roundtrip():
    key = bytes(range(32))
    tx = CipherState(key)
    rx = CipherState(key)
    ciphertext = tx.encrypt_with_ad(b"ad", b"hello")
    assert rx.decrypt_with_ad(b"ad", ciphertext) == b"hello"


def test_noise_nonce_big_endian():
    assert nonce12(1) == b"\x00" * 11 + b"\x01"
    assert nonce12(0x0102030405060708)[4:] == bytes.fromhex("0102030405060708")


def test_hkdf_is_deterministic():
    a = hkdf_noise(b"a" * 32, b"b" * 32)
    b = hkdf_noise(b"a" * 32, b"b" * 32)
    assert a == b
    assert all(len(item) == 32 for item in a)


def test_noise_protocol_name_initialization():
    # Noise spec: protocol names <= HASHLEN are zero-padded to HASHLEN.
    assert len(PROTOCOL) < 32
    state = SymmetricState()
    assert len(state.ck) == 32
    assert len(state.h) == 32

    initiator = NoiseXXInitiator()
    message1 = initiator.write_message1(b"")
    assert len(message1) == 32


def test_noise_fragment_reassembly():
    raw = b"x" * 140_000
    chunks = split_noise_payload(raw)
    assert len(chunks) == 3
    reassembler = NoiseReassembler()
    result = None
    for chunk in chunks:
        result = reassembler.feed(chunk)
    assert result == raw


def test_gateway_path_normalization():
    assert normalize_gateway_path(r"C:\Users\alice\workspace\user\files\a.mp4") == "workspace/user/files/a.mp4"
    assert normalize_gateway_path("/home/alice/workspace/user/files/a.mp4") == "workspace/user/files/a.mp4"
    assert normalize_gateway_path("file:///workspace/user/files/a.mp4?x=1") == "workspace/user/files/a.mp4"


@pytest.mark.asyncio
async def test_video_download_404_retries_and_preserves_success(tmp_path, monkeypatch):
    from muse_ai.client import MuseClient
    client = MuseClient(auth=None, state_dir=tmp_path)
    refs = [
        VideoRef(path="workspace/imagine_media/one.mp4"),
        VideoRef(path="workspace/imagine_media/two.mp4"),
    ]
    calls: dict[str, int] = {}

    async def fake_sessions_list():
        return {"result": {"sessions": []}}

    async def fake_history(*, session_id=None, limit=80, **kwargs):
        return {"messages": []}

    async def fake_stream(**kwargs):
        return [{"session_id": "s-video"}]

    async def fake_wait(**kwargs):
        return refs, {"messages": []}

    async def fake_download(ref, destination):
        calls[ref.identity] = calls.get(ref.identity, 0) + 1
        if ref.identity.endswith("two.mp4") and calls[ref.identity] == 1:
            raise RuntimeError("Hatch RPC returned HTTP 404: not_found")
        path = Path(destination)
        path.write_bytes(b"mp4-example")
        return path

    async def instant_sleep(*args):
        return None

    monkeypatch.setattr(client, "sessions_list", fake_sessions_list)
    monkeypatch.setattr(client, "history", fake_history)
    monkeypatch.setattr(client, "chat_stream", fake_stream)
    monkeypatch.setattr(client, "wait_for_videos", fake_wait)
    monkeypatch.setattr(client, "download_video", fake_download)
    monkeypatch.setattr("muse_ai.client.asyncio.sleep", instant_sleep)
    result = await client.generate_video(
        prompt="Generate 2 videos", output_dir=tmp_path / "output"
    )
    assert len(result.downloaded) == 2
    assert result.download_errors == []
    assert calls[refs[0].identity] == 1
    assert calls[refs[1].identity] == 2


def test_media_extraction_prefers_stable_path():
    data = {
        "session_id": "session-1",
        "media": {
            "kind": "video",
            "mime_type": "video/mp4",
            "path": "workspace/user/files/result.mp4",
            "resource_id": "resource-1",
            "media_handle": "media-handle-1",
            "variants": {
                "original": "https://cdn.example.invalid/result.mp4?grant=one"
            },
        },
    }
    refs = extract_video_refs(data)
    assert len(refs) == 1
    assert refs[0].identity == "workspace/user/files/result.mp4"
    assert refs[0].media_handle == "media-handle-1"
    assert extract_session_ids(data) == ["session-1"]


def test_extract_generated_image_presentation():
    data = {
        "session_id": "session-image-1",
        "presentations": [
            {
                "kind": "image",
                "presentationId": "presentation-1",
                "data": {
                    "images": [
                        {
                            "path": "sandbox://workspace/user/files/generated.png",
                            "label": "generated.png",
                            "mime": "image/png",
                            "media_handle": "image-handle-1",
                            "width": 1024,
                            "height": 1024,
                            "variants": {
                                "original": "/api/idea-cards/media/image-handle-1"
                            },
                        }
                    ]
                },
            }
        ],
    }
    refs = extract_image_refs(data)
    assert len(refs) == 1
    assert refs[0].path == "sandbox://workspace/user/files/generated.png"
    assert refs[0].media_handle == "image-handle-1"
    assert refs[0].mime_type == "image/png"
    assert refs[0].label == "generated.png"
    assert refs[0].width == 1024
    assert refs[0].height == 1024
    assert refs[0].url == "/api/idea-cards/media/image-handle-1"


def test_build_inline_image_item(tmp_path: Path):
    path = tmp_path / "image.png"
    Image.new("RGB", (16, 16), (10, 20, 30)).save(path)
    item = build_image_item(path)
    assert item["type"] == "image"
    assert item["mime_type"] == "image/png"
    assert base64.b64decode(item["data_base64"]).startswith(b"\x89PNG")


def test_build_text_only_items():
    items = build_items([], "Create a cinematic 10-second video")
    assert items == [
        {"type": "text", "text": "Create a cinematic 10-second video"}
    ]

@pytest.mark.asyncio
async def test_send_text_starts_new_secondary_thread_not_primary(tmp_path, monkeypatch):
    client = MuseClient(auth=None, state_dir=tmp_path)
    sent_sessions = []
    history_sessions = []

    async def fake_stream(*, prompt, session_id, stream_timeout):
        assert session_id
        sent_sessions.append(session_id)
        return [{"session_id": session_id, "is_thread": True, "channel": "ack"}]

    async def fake_history(*, session_id=None, limit=80, **kwargs):
        history_sessions.append(session_id)
        if len(history_sessions) in (1, 3):
            return {"messages": []}
        return {
            "messages": [
                {"role": "assistant", "content": "Chào bạn!"}
            ]
        }

    async def fail_session_list():
        raise AssertionError("No primary-session fallback is allowed")

    monkeypatch.setattr(client, "chat_stream", fake_stream)
    monkeypatch.setattr(client, "history", fake_history)
    monkeypatch.setattr(client, "sessions_list", fail_session_list)

    result = await client.send_text(prompt="xin chào", session_id=None, timeout=5, settle_seconds=0)
    assert result.text == "Chào bạn!"
    assert result.session_id == sent_sessions[0]
    assert history_sessions == [sent_sessions[0], sent_sessions[0]]

    # An existing thread must be reused, not replaced with a fresh ID.
    second = await client.send_text(
        prompt="tiếp tục", session_id=result.session_id, timeout=5, settle_seconds=0
    )
    assert second.session_id == result.session_id
    assert sent_sessions == [result.session_id, result.session_id]


@pytest.mark.asyncio
async def test_send_text_waits_for_last_history_snapshot(tmp_path, monkeypatch):
    client = MuseClient(auth=None, state_dir=tmp_path)
    polls = 0

    async def fake_stream(*, prompt, session_id, stream_timeout):
        return [{"session_id": session_id, "channel": "ack"}]

    async def fake_history(*, session_id=None, limit=80, **kwargs):
        nonlocal polls
        polls += 1
        if polls == 1:
            return {"messages": []}
        answer = "Dạ bẩm" if polls <= 3 else "Dạ bẩm, con biết người ngài hỏi."
        return {"messages": [{"role": "assistant", "content": answer}]}

    monkeypatch.setattr(client, "chat_stream", fake_stream)
    monkeypatch.setattr(client, "history", fake_history)

    result = await client.send_text(
        prompt="con biết không",
        timeout=3,
        poll_interval=0.005,
        settle_seconds=0.025,
    )
    assert result.text == "Dạ bẩm, con biết người ngài hỏi."
    assert polls >= 4


@pytest.mark.asyncio
async def test_wait_for_videos_reports_final_muse_text_without_video(tmp_path, monkeypatch):
    from muse_ai.errors import MuseVideoNotCreated

    client = MuseClient(auth=None, state_dir=tmp_path)
    polls = 0

    async def finished_history(*, session_id=None, limit=80, **kwargs):
        nonlocal polls
        polls += 1
        return {"messages": [
            {"role": "assistant",
             "content": "Xin lỗi, công cụ tạo video không khả dụng.",
             "finish_reason": "stop"}
        ], "tasks": [{"id": "task-new", "kind": "video_generation", "status": "failed"}]}

    monkeypatch.setattr(client, "history", finished_history)
    with pytest.raises(MuseVideoNotCreated) as error:
        await client.wait_for_videos(
            session_id="thread-1", baseline=set(), timeout=2,
            poll_interval=0.001, baseline_assistant=set(),
        )
    assert polls == 2
    assert "Xin lỗi, công cụ tạo video" in str(error.value)
    assert error.value.session_id == "thread-1"


@pytest.mark.asyncio
async def test_wait_for_videos_does_not_fail_while_rendering(tmp_path, monkeypatch):
    client = MuseClient(auth=None, state_dir=tmp_path)
    polls = 0

    async def history(*, session_id=None, limit=80, **kwargs):
        nonlocal polls
        polls += 1
        response = {"messages": [
            {"role": "assistant", "content": "Đang tạo video", "finish_reason": "stop"}
        ], "tasks": [{"kind": "video_generation",
                      "status": "running" if polls < 3 else "completed"}]}
        if polls >= 3:
            response["media"] = {"kind": "video", "path": "workspace/result.mp4"}
        return response

    monkeypatch.setattr(client, "history", history)
    refs, _ = await client.wait_for_videos(
        session_id="thread-1", baseline=set(), timeout=2,
        poll_interval=0.001, settle_seconds=0, baseline_assistant=set(),
    )
    assert polls >= 3
    assert refs[0].path == "workspace/result.mp4"


@pytest.mark.asyncio
async def test_send_text_rejects_wrong_thread_ack(tmp_path, monkeypatch):
    client = MuseClient(auth=None, state_dir=tmp_path)

    async def fake_stream(*, prompt, session_id, stream_timeout):
        return [{"session_id": "existing-primary-thread", "is_primary": True}]

    async def fake_history(*, session_id=None, limit=80, **kwargs):
        return {"messages": []}

    monkeypatch.setattr(client, "chat_stream", fake_stream)
    monkeypatch.setattr(client, "history", fake_history)

    with pytest.raises(RuntimeError, match="different session_id"):
        await client.send_text(prompt="new chat", session_id=None, timeout=5)


@pytest.mark.asyncio
async def test_resolve_session_id_never_uses_arbitrary_existing_thread(
    tmp_path, monkeypatch
):
    client = MuseClient(auth=None, state_dir=tmp_path)

    async def fake_list():
        return {"result": {"sessions": [
            {"session_id": "primary-existing", "is_primary": True}
        ]}}

    monkeypatch.setattr(client, "sessions_list", fake_list)
    resolved = await client._resolve_session_id(
        explicit=None, events=[], sessions_before={"primary-existing"}
    )
    assert resolved is None

