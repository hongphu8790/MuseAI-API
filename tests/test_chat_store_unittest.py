"""Regression tests for durable chat history; stdlib only, runnable without pytest."""
import tempfile
import unittest
from pathlib import Path

# Load this pure-stdlib module without importing optional Muse networking dependencies.
import importlib.util
spec = importlib.util.spec_from_file_location(
    "chat_store", Path(__file__).resolve().parents[1] / "muse_ai/web/chat_store.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
ChatStore, merge_messages, parse_muse_history = (
    module.ChatStore, module.merge_messages, module.parse_muse_history
)
text_spec = importlib.util.spec_from_file_location(
    "muse_text", Path(__file__).resolve().parents[1] / "muse_ai/text.py"
)
text_module = importlib.util.module_from_spec(text_spec)
text_spec.loader.exec_module(text_module)


class ChatHistoryTests(unittest.TestCase):
    def test_roundtrip_survives_restart_and_preserves_all_messages(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.sqlite3"
            data = {
                "id": "thread-1", "accountId": "account-1",
                "sessionId": "muse-123", "name": "Chào Muse",
                "messages": [
                    {"role": "user", "text": f"message {n}"} for n in range(180)
                ],
            }
            first = ChatStore(path)
            first.put(data)
            restored = ChatStore(path).get("thread-1")
            self.assertEqual(len(restored["messages"]), 180)
            self.assertEqual(restored["sessionId"], "muse-123")
            self.assertEqual(ChatStore(path).list("account-2"), [])

    def test_sync_upgrades_partial_answer_and_keeps_local_attachments(self):
        local = [
            {"role": "user", "text": "Ai vậy?"},
            {"role": "assistant", "text": "Dạ bẩm", "files": [{"url": "/api/attached"}]},
        ]
        remote = [
            {"role": "user", "text": "Ai vậy?"},
            {"role": "assistant", "text": "Dạ bẩm, con đã biết câu trả lời dài."},
        ]
        result = merge_messages(local, remote)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[-1]["text"], remote[-1]["text"])
        self.assertEqual(len(result[-1]["files"]), 1)

    def test_server_file_ids_survive_browser_snapshot_and_muse_sync(self):
        local = [{"role": "user", "text": "Hãy xem 2 tệp",
                  "uploads": [{"id": "a" * 32, "name": "image.png"},
                              {"id": "b" * 32, "name": "guide.pdf"}]}]
        browser = [{"role": "user", "text": "Hãy xem 2 tệp",
                    "uploads": [{"name": "image.png"}, {"name": "guide.pdf"}]}]
        merged = merge_messages(local, browser)
        self.assertEqual(len(merged), 1)
        self.assertEqual([f["id"] for f in merged[0]["uploads"]],
                         ["a" * 32, "b" * 32])
        remote = [{"role": "user",
                   "text": "Hãy xem 2 tệp\n\nCác tài liệu đã tải lên workspace Muse."}]
        again = merge_messages(merged, remote)
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["text"], "Hãy xem 2 tệp")
        self.assertEqual(len(again[0]["uploads"]), 2)

    def test_sync_keeps_messages_missing_from_remote_limited_history(self):
        old = [{"role": "user", "text": "old turn"}]
        latest = [{"role": "user", "text": "new turn"}]
        result = merge_messages(old, latest)
        self.assertEqual([m["text"] for m in result], ["old turn", "new turn"])

    def test_account_and_session_cannot_be_reassigned(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ChatStore(Path(folder) / "history.db")
            first = {"id": "thread", "accountId": "A",
                     "sessionId": "s1", "messages": []}
            store.put(first)
            with self.assertRaisesRegex(ValueError, "different Muse account"):
                store.put({**first, "accountId": "B"})
            with self.assertRaisesRegex(ValueError, "Cannot change"):
                store.put({**first, "sessionId": "s2"})

    def test_explicit_stream_completion_marker_only(self):
        partial = {"messages": [
            {"role": "assistant", "content": "Dạ bẩm", "status": "streaming"}
        ]}
        finished = {"messages": [
            {"role": "assistant", "content": "Dạ bẩm đầy đủ",
             "finish_reason": "stop"}
        ]}
        self.assertFalse(text_module.assistant_response_finished(partial))
        self.assertTrue(text_module.assistant_response_finished(finished))

    def test_completed_chat_can_have_running_video_task(self):
        payload = {"messages": [
            {"role": "assistant", "content": "Đang tạo video", "finish_reason": "stop"}
        ], "tasks": [
            {"id": "job-1", "kind": "video_generation", "status": "rendering"}
        ]}
        self.assertTrue(text_module.assistant_response_finished(payload))
        self.assertTrue(text_module.muse_video_task_pending(payload))
        payload["tasks"][0]["status"] = "completed"
        self.assertFalse(text_module.muse_video_task_pending(payload))
        self.assertTrue(text_module.muse_video_task_terminal(payload))
        previous_tasks = {"job-1:completed"}
        self.assertFalse(
            text_module.terminal_video_task_ids(payload) - previous_tasks,
            "An old terminal job must not trigger failure for the new message"
        )
        payload["tasks"].append({
            "id": "job-2", "kind": "video_generation", "status": "failed"
        })
        self.assertEqual(
            text_module.terminal_video_task_ids(payload) - previous_tasks,
            {"job-2:failed"}
        )
        self.assertFalse(text_module.muse_video_task_terminal({
            "messages": [{"role": "assistant", "status": "completed"}]
        }))
        self.assertFalse(text_module.muse_video_task_pending({
            "messages": [{"role": "assistant", "content": "running",
                          "status": "running"}]
        }))

    def test_parser_supports_nested_text_blocks_and_stream_snapshots(self):
        remote = {"result": {"messages": [
            {"id": "u1", "role": "user", "content": [{"type": "text", "text": "Xin chào"}]},
            {"id": "a1", "role": "assistant", "content": "Dạ bẩm"},
            {"id": "a1", "role": "assistant",
             "content": [{"type": "text", "text": "Dạ bẩm, câu đầy đủ."}]},
        ]}}
        self.assertEqual(parse_muse_history(remote), [
            {"role": "user", "text": "Xin chào"},
            {"role": "assistant", "text": "Dạ bẩm, câu đầy đủ."},
        ])
        self.assertEqual(parse_muse_history({"result": {"metadata": "hello"}}), [])


if __name__ == "__main__":
    unittest.main()
