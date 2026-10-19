import asyncio
import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

import server


class LogHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "logs" / "panel.jsonl"
        self.hub = server.Hub(self.path)

    async def test_restart_preserves_timestamp_account_and_only_log_fields(self):
        await self.hub.push_account(
            "account-one", "log", account_name="测试账号", text="已提交答案 A",
            level="info", ts=1792421234.5, cookies={"token": "cookie-secret"},
            api_key="api-secret", password="password-secret",
        )
        await self.hub.push("state", user_id="state-only")

        restored = server.Hub(self.path)
        message = json.loads(restored.log_history[0])
        self.assertEqual(message["ts"], 1792421234.5)
        self.assertEqual(message["account_id"], "account-one")
        self.assertEqual(message["account_name"], "测试账号")
        self.assertEqual(message["text"], "已提交答案 A")
        self.assertEqual(
            message["log_id"], json.loads(self.hub.log_history[0])["log_id"],
        )
        persisted = self.path.read_text(encoding="utf-8")
        for private_value in ("cookie-secret", "api-secret", "password-secret", "state-only"):
            self.assertNotIn(private_value, persisted)
        self.assertEqual(len(persisted.splitlines()), 1)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    async def test_status_events_cannot_evict_logs(self):
        await self.hub.push("log", text="上一节课的记录", level="info")
        for index in range(500):
            await self.hub.push("state", tick=index)
            await self.hub.push("problem_timing", problem_id="one", remaining=index)

        self.assertEqual(len(self.hub.history), 400)
        self.assertFalse(any(json.loads(item)["kind"] == "log" for item in self.hub.history))
        self.assertEqual(json.loads(self.hub.log_history[0])["text"], "上一节课的记录")
        self.assertEqual(len(server.Hub(self.path).log_history), 1)

    async def test_count_limit_keeps_newest_records_after_restart(self):
        with mock.patch.object(server.Hub, "LOG_LIMIT", 3):
            for index in range(8):
                await self.hub.push("log", text=f"记录 {index}")
                self.assertLessEqual(len(self.path.read_bytes().splitlines()), 3)
            restored = server.Hub(self.path)

        self.assertEqual(
            [json.loads(item)["text"] for item in restored.log_history],
            ["记录 5", "记录 6", "记录 7"],
        )

    async def test_byte_limit_counts_utf8_and_keeps_newest_records(self):
        with mock.patch.object(server.Hub, "LOG_MAX_BYTES", 900):
            for index in range(10):
                await self.hub.push("log", text=f"{index} " + "题" * 120)
                self.assertLessEqual(self.path.stat().st_size, 900)
                self.assertLessEqual(self.hub._log_history_bytes, 900)
            restored = server.Hub(self.path)

        self.assertTrue(restored.log_history)
        self.assertTrue(json.loads(restored.log_history[-1])["text"].startswith("9 "))
        self.assertEqual(list(restored.log_history), list(self.hub.log_history))

    async def test_corrupt_and_partial_records_do_not_hide_valid_logs(self):
        self.path.parent.mkdir()
        self.path.write_bytes(
            b"not-json\n"
            + json.dumps({"kind": "log", "text": "完整记录", "ts": 123.5}).encode("utf-8")
            + b"\n"
            + json.dumps({"kind": "state", "ts": 124, "password": "private"}).encode("utf-8")
            + b"\n{\"kind\": \"log\", \"text\":"
        )

        restored = server.Hub(self.path)
        await restored.push("log", text="重启后的记录")
        reloaded = server.Hub(self.path)

        self.assertEqual(
            [json.loads(item)["text"] for item in reloaded.log_history],
            ["完整记录", "重启后的记录"],
        )
        self.assertNotIn("private", self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(self.path.read_bytes().splitlines()), 2)

    async def test_oversized_existing_file_is_read_and_compacted_within_limit(self):
        self.path.parent.mkdir()
        records = [
            json.dumps({"kind": "log", "text": str(index) + "题" * 60, "ts": index})
            for index in range(50)
        ]
        self.path.write_text("\n".join(records) + "\n", encoding="utf-8")

        with mock.patch.object(server.Hub, "LOG_MAX_BYTES", 1000):
            restored = server.Hub(self.path)

        self.assertLessEqual(self.path.stat().st_size, 1000)
        self.assertTrue(json.loads(restored.log_history[-1])["text"].startswith("49"))

    async def test_storage_failure_does_not_interrupt_clients_and_recovers_missing_logs(self):
        websocket = mock.Mock()
        websocket.send_str = mock.AsyncMock()
        self.hub.clients.add(websocket)
        with mock.patch.object(self.hub, "_append_log_file", side_effect=OSError("disk full")), \
                mock.patch("builtins.print"):
            await self.hub.push("log", text="写盘失败时的记录")
            await self.hub.push_account("one", "log", text="另一条记录", account_name="账号")
            await self.hub.push("state", phase="in_class")

        self.assertEqual(websocket.send_str.await_count, 3)
        self.assertEqual(len(self.hub.log_history), 2)
        await self.hub.push("log", text="磁盘恢复后的记录")
        restored = server.Hub(self.path)
        self.assertEqual(
            [json.loads(item)["text"] for item in restored.log_history],
            ["写盘失败时的记录", "另一条记录", "磁盘恢复后的记录"],
        )

    async def test_slow_disk_does_not_block_status_events(self):
        started = threading.Event()
        release = threading.Event()
        original = self.hub._append_log_file

        def slow_append(*args):
            started.set()
            if not release.wait(2):
                raise TimeoutError("test disk wait expired")
            original(*args)

        with mock.patch.object(self.hub, "_append_log_file", side_effect=slow_append):
            log_task = asyncio.create_task(self.hub.push("log", text="后台写盘"))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                await asyncio.wait_for(self.hub.push("state", phase="in_class"), timeout=0.5)
                self.assertFalse(log_task.done())
                self.assertEqual(json.loads(self.hub.history[-1])["kind"], "state")
            finally:
                release.set()
                await log_task

    async def test_concurrent_account_logs_are_written_once_in_replay_order(self):
        await asyncio.gather(*(
            server.AccountHub(self.hub, f"account-{index}", f"账号 {index}").push(
                "log", text=f"答案 {index}",
            )
            for index in range(20)
        ))

        restored = server.Hub(self.path)
        self.assertEqual(len(restored.log_history), 20)
        self.assertEqual(list(restored.log_history), list(self.hub.log_history))
        self.assertEqual(len({json.loads(item)["log_id"] for item in restored.log_history}), 20)

    async def test_cancelled_compaction_finishes_before_a_newer_log_can_write(self):
        started = threading.Event()
        release = threading.Event()
        writes = []
        original = self.hub._replace_log_file

        def slow_first_replace(messages):
            writes.append(json.loads(messages[-1])["text"])
            if len(writes) == 1:
                started.set()
                if not release.wait(2):
                    raise TimeoutError("test disk wait expired")
            original(messages)

        with mock.patch.object(server.Hub, "LOG_LIMIT", 1):
            await self.hub.push("log", text="此前的记录")
            with mock.patch.object(self.hub, "_replace_log_file", side_effect=slow_first_replace):
                first = asyncio.create_task(self.hub.push("log", text="取消时仍在写盘的记录"))
                second = None
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 1))
                    first.cancel()
                    await asyncio.sleep(0)
                    first.cancel()  # Repeated cancellation must not release the disk lock.
                    second = asyncio.create_task(self.hub.push("log", text="较新的记录"))
                    await asyncio.sleep(0.02)
                    self.assertFalse(first.done())
                    self.assertFalse(second.done())
                    self.assertEqual(writes, ["取消时仍在写盘的记录"])
                finally:
                    release.set()
                    results = await asyncio.gather(
                        *([first, second] if second is not None else [first]),
                        return_exceptions=True,
                    )
                self.assertIsInstance(results[0], asyncio.CancelledError)
                self.assertIsNone(results[1])
            restored = server.Hub(self.path)

        self.assertEqual(writes, ["取消时仍在写盘的记录", "较新的记录"])
        self.assertEqual([json.loads(item)["text"] for item in restored.log_history], ["较新的记录"])

    async def test_cleanup_drains_queued_global_and_account_logs_with_slow_disk(self):
        started = threading.Event()
        release = threading.Event()
        original = self.hub._append_log_file

        def slow_append(*args):
            started.set()
            if not release.wait(2):
                raise TimeoutError("test disk wait expired")
            original(*args)

        async def stop_accounts():
            self.hub.log("关闭时的全局记录")
            self.hub.log_account("one", "账号", "关闭时的账号记录")
            self.hub.log("最后一条记录")

        manager = mock.Mock()
        manager.shutdown = mock.AsyncMock(side_effect=stop_accounts)
        with mock.patch.object(server, "hub", self.hub), \
                mock.patch.object(server, "account_manager", manager), \
                mock.patch.object(server, "_cancel_restore", new=mock.AsyncMock()), \
                mock.patch.object(server, "_supersede_qr_attempt", new=mock.AsyncMock()), \
                mock.patch.object(server, "llm_session", None), \
                mock.patch.object(self.hub, "_append_log_file", side_effect=slow_append), \
                mock.patch("builtins.print"):
            cleanup = asyncio.create_task(server.on_cleanup({}))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                self.assertFalse(cleanup.done())
                self.assertEqual(len(self.hub._log_tasks), 3)
                cleanup.cancel()  # Cleanup cancellation must also wait for queued logs.
                await asyncio.sleep(0)
                self.assertFalse(cleanup.done())
            finally:
                release.set()
                result = await asyncio.gather(cleanup, return_exceptions=True)

        self.assertIsInstance(result[0], asyncio.CancelledError)
        self.assertFalse(self.hub._log_tasks)
        restored = server.Hub(self.path)
        self.assertEqual(
            [json.loads(item)["text"] for item in restored.log_history],
            ["关闭时的全局记录", "关闭时的账号记录", "最后一条记录"],
        )

    async def test_cleanup_accepts_legacy_hubs_without_log_drain(self):
        with mock.patch.object(server, "hub", object()), \
                mock.patch.object(server, "account_manager", None), \
                mock.patch.object(server, "watcher", None), \
                mock.patch.object(server, "llm_session", None), \
                mock.patch.object(server, "_cancel_restore", new=mock.AsyncMock()), \
                mock.patch.object(server, "_supersede_qr_attempt", new=mock.AsyncMock()):
            await server.on_cleanup({})


class LogReplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "panel.jsonl"
        self.hub = server.Hub(self.path)
        for patch in (
            mock.patch.object(server, "hub", self.hub),
            mock.patch.object(server, "account_manager", None),
            mock.patch.object(server, "public_state", return_value={"phase": "idle"}),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        app = server.web.Application()
        app["runtime_config"] = server.server_runtime_config({})
        app.router.add_get("/ws", server.ws_handler)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_websocket_replays_original_logs_despite_status_flood_and_reconnect(self):
        await self.hub.push_account(
            "one", "log", account_name="账号", text="此前的答题记录", ts=1234.5,
        )
        for index in range(500):
            await self.hub.push("state", tick=index)

        for _ in range(2):
            websocket = await self.client.ws_connect("/ws")
            try:
                log = await websocket.receive_json(timeout=1)
                state = await websocket.receive_json(timeout=1)
                self.assertEqual(log["kind"], "log")
                self.assertEqual(log["ts"], 1234.5)
                self.assertEqual(log["account_name"], "账号")
                self.assertEqual(log["log_id"], json.loads(self.hub.log_history[0])["log_id"])
                self.assertEqual(state["kind"], "state")
            finally:
                await websocket.close()

    async def test_websocket_replays_logs_restored_from_disk(self):
        await self.hub.push("log", text="服务重启前的记录", ts=2345.6)
        restored = server.Hub(self.path)
        with mock.patch.object(server, "hub", restored):
            websocket = await self.client.ws_connect("/ws")
            try:
                log = await websocket.receive_json(timeout=1)
                self.assertEqual(log["text"], "服务重启前的记录")
                self.assertEqual(log["ts"], 2345.6)
            finally:
                await websocket.close()


class LogFrontendTests(unittest.TestCase):
    def test_replayed_logs_use_original_date_and_deduplicate_reconnects(self):
        html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("message.account_name, message.ts, message.log_id", html)
        self.assertIn("new Date(timestamp * 1000)", html)
        self.assertIn('date.toLocaleString("zh-CN", {year:"numeric", month:"2-digit", day:"2-digit"', html)
        self.assertIn("if (logId && seenLogIds.has(logId)) return", html)
        self.assertIn("seenLogIds.size > 2000", html)
        self.assertIn("log.childNodes.length > 1000", html)


if __name__ == "__main__":
    unittest.main()
