from __future__ import annotations

import base64
from pathlib import Path

from PIL import Image

from muse_ai.attachments import build_image_item, build_items
from muse_ai.filesystem import normalize_gateway_path
from muse_ai.media import extract_session_ids, extract_video_refs
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


def test_media_extraction_merges_bare_label_with_sandbox_path():
    # Muse lists one generated file as a bare label (resources[].label), a
    # sandbox:// uri and a widget with a signed variants.original URL; a
    # second, separate VideoRef for the bare label 404s on fs.raw.
    name = "media-generation-demo-0-f1201613.mp4"
    data = {
        "chat_events": [
            {"resources": [{"kind": "video", "label": name, "mime_type": "video/mp4",
                            "uri": f"sandbox://workspace/imagine_media/{name}"}]},
            {"payload": {"data": {"videos": [{
                "label": "Demo", "mime": "video/mp4",
                "path": f"sandbox://workspace/imagine_media/{name}",
                "variants": {"original": f"https://vm.metaaivm.com/media/raw/workspace/imagine_media/{name}"},
            }]}}},
        ]
    }
    refs = extract_video_refs(data)
    assert len(refs) == 1
    assert refs[0].path == f"sandbox://workspace/imagine_media/{name}"
    assert refs[0].url.startswith("https://")


def test_subscription_usage_parsed_from_flight_rows():
    from muse_ai.usage import parse_subscription_payload, usage_from_subscription

    text = (
        '0:{"a":"$@1","f":"","q":"","i":false}\n'
        '1:{"success":true,"subscription":{"tier":{"tierId":"hatch_free","name":"Muse Free",'
        '"tierCode":"HATCH_FREE"},"usage":{"state":"METERED","percentUsed":2,"resetsAt":1793026047,'
        '"quotaStatus":"SUFFICIENT"},"topupRowValueLabel":"0% used (1B tokens left)"}}\n'
    )
    usage = usage_from_subscription(parse_subscription_payload(text))
    assert (usage.plan, usage.tier_code, usage.percent_used, usage.quota_status) == (
        "Muse Free", "HATCH_FREE", 2, "SUFFICIENT")
    assert usage.resets_at is not None and usage.resets_at.year == 2026
    assert "weekly used=2%" in usage.summary()
