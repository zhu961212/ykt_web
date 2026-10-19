import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import server


class _Hub:
    account_id = "deadline-test"

    def __init__(self):
        self.state = {}
        self.events = []
        self.logs = []

    def log(self, text, level="info"):
        self.logs.append((level, text))

    async def sync_state(self):
        pass

    async def push(self, kind, **values):
        self.events.append((kind, values))

    def clear_problem(self):
        pass


class _GatedSolver:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.cancelled = False

    async def solve(self, _problem):
        self.calls += 1
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return {"result": ["A"], "display": "A"}


class DeadlineExtensionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = server.SharedAnswerPool()
        self.solver = _GatedSolver()
        self.watchers = []

    async def asyncTearDown(self):
        for watcher in self.watchers:
            await watcher.stop()
        await self.pool.clear()

    def prepare_watcher(self):
        watcher = server.Watcher(
            {"bot": {"dry_run": True, "answer_delay_seconds": 0,
                     "request_margin_seconds": 0.5}, "llm": {}},
            SimpleNamespace(server_key="test", bearer="", lesson_token=""),
            self.solver, _Hub(), self.pool,
        )
        watcher.running = True
        watcher.lesson_id = "lesson"
        watcher.problems["p"] = {
            "problem_id": "p", "type": 1, "title": "Question",
            "options": [("A", "One"), ("B", "Two")],
        }
        now = time.time()
        watcher.question_windows["p"] = {
            "start": now, "dt_ms": int(now * 1000), "limit": 3,
            "request_margin": 0.5, "closes_at": now + 3, "submit_by": now + 2.5,
            "lesson_id": "lesson",
        }
        self.watchers.append(watcher)
        return watcher

    @staticmethod
    def start_flow(watcher):
        task = asyncio.create_task(watcher._answer_flow("p"))
        watcher.answer_tasks["p"] = task
        return task

    async def test_extension_preserves_one_solve_past_original_deadline(self):
        original_wait = asyncio.wait
        budgets = []
        with mock.patch.object(server.time, "time", return_value=1_700_000_000):
            watcher = self.prepare_watcher()

            async def wait_with_extension(tasks, *, timeout):
                budgets.append(timeout)
                await self.solver.started.wait()
                if len(budgets) == 1:
                    watcher._extend_problem({"prob": "p", "extend": 5})
                    return set(), set(tasks)
                self.solver.release.set()
                return await original_wait(tasks, timeout=1)

            with mock.patch.object(server.asyncio, "wait", new=wait_with_extension):
                await self.start_flow(watcher)

        self.assertEqual(budgets, [2.5, 7.5])
        self.assertEqual(self.solver.calls, 1)
        self.assertIn("p", watcher.answered)

    async def test_repeated_extensions_only_expand_the_existing_wait(self):
        original_wait = asyncio.wait
        budgets = []
        with mock.patch.object(server.time, "time", return_value=1_700_000_000):
            watcher = self.prepare_watcher()

            async def wait_with_extensions(tasks, *, timeout):
                budgets.append(timeout)
                await self.solver.started.wait()
                if len(budgets) <= 2:
                    watcher._extend_problem({"prob": "p", "extend": 5})
                    return set(), set(tasks)
                self.solver.release.set()
                return await original_wait(tasks, timeout=1)

            with mock.patch.object(server.asyncio, "wait", new=wait_with_extensions):
                await self.start_flow(watcher)

        self.assertEqual(budgets, [2.5, 7.5, 12.5])
        self.assertEqual(self.solver.calls, 1)
        self.assertIn("p", watcher.answered)

    async def test_unchanged_deadline_uses_random_preview_without_restarting_solve(self):
        watcher = self.prepare_watcher()
        waiters = []

        async def expire_wait(tasks, *, timeout):
            waiters.extend(tasks)
            await self.solver.started.wait()
            return set(), set(tasks)

        with mock.patch.object(server.asyncio, "wait", new=expire_wait):
            await self.start_flow(watcher)

        self.assertEqual(len(waiters), 1)
        self.assertTrue(waiters[0].cancelled())
        self.assertEqual(self.solver.calls, 1)
        self.assertIn("p", watcher.answered)
        self.assertIn(
            "dry",
            [values.get("status") for kind, values in watcher.hub.events if kind == "problem"],
        )
        self.assertTrue(any(values.get("source") == "random_timeout"
                            for kind, values in watcher.hub.events if kind == "problem"))

    async def test_cancellation_cleans_waiter_and_preserves_other_account_solve(self):
        first = self.prepare_watcher()
        second = self.prepare_watcher()
        original_wait = asyncio.wait
        waiters = []
        both_waiting = asyncio.Event()

        async def observe_wait(tasks, *, timeout):
            waiters.extend(tasks)
            if len(waiters) == 2:
                both_waiting.set()
            return await original_wait(tasks, timeout=timeout)

        with mock.patch.object(server.asyncio, "wait", new=observe_wait):
            first_task = self.start_flow(first)
            second_task = self.start_flow(second)
            await asyncio.wait_for(both_waiting.wait(), timeout=1)
            await asyncio.wait_for(self.solver.started.wait(), timeout=1)
            await first.stop()
            self.assertTrue(first_task.cancelled())
            self.assertTrue(waiters[0].cancelled())
            self.assertFalse(self.solver.cancelled)
            self.solver.release.set()
            await asyncio.wait_for(second_task, timeout=1)

        self.assertEqual(self.solver.calls, 1)
        self.assertIn("p", second.answered)
        self.assertFalse(self.solver.cancelled)

    async def test_real_timeout_expires_without_extension(self):
        finished = asyncio.Event()

        async def pending_operation():
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        with self.assertRaises(asyncio.TimeoutError):
            await server.Watcher._wait_for_question_window(
                pending_operation(), {"submit_by": time.time() + 0.02},
            )
        self.assertTrue(finished.is_set())

    async def test_solve_logs_model_image_budget_and_monotonic_elapsed(self):
        watcher = self.prepare_watcher()
        clock = SimpleNamespace(time=mock.Mock(return_value=watcher.question_windows["p"]["start"]),
                                monotonic=mock.Mock(side_effect=[100.0, 100.25]))
        self.solver.release.set()
        with mock.patch.object(server, "time", new=clock):
            watcher.cfg["llm"] = {
                "model": "vision-test-model", "vision_enabled": True,
                "api_key": "never-print-this-secret",
            }
            watcher.problems["p"]["images"] = ["https://example.invalid/question.png"]
            await self.start_flow(watcher)

        logs = "\n".join(message for _, message in watcher.hub.logs)
        self.assertIn("开始解题：模型=vision-test-model，图片题=是，可用预算=2.50s", logs)
        self.assertIn("解题完成，耗时=0.25s，有效预算=2.50s，距模型等待截止=2.50s", logs)
        self.assertNotIn("never-print-this-secret", logs)

    async def test_timeout_logs_elapsed_and_budget_after_extension(self):
        watcher = self.prepare_watcher()
        initial_time = watcher.question_windows["p"]["start"]
        wall_clock = [initial_time]
        clock = SimpleNamespace(time=lambda: wall_clock[0],
                                monotonic=lambda: wall_clock[0] - initial_time + 100)
        wait_count = 0
        with mock.patch.object(server, "time", new=clock):
            watcher.cfg["llm"]["model"] = "timeout-test-model"

            async def expire_after_extension(tasks, *, timeout):
                nonlocal wait_count
                wait_count += 1
                await self.solver.started.wait()
                if wait_count == 1:
                    watcher._extend_problem({"prob": "p", "extend": 5})
                wall_clock[0] += timeout
                return set(), set(tasks)

            with mock.patch.object(server.asyncio, "wait", new=expire_after_extension):
                await self.start_flow(watcher)

        logs = "\n".join(message for _, message in watcher.hub.logs)
        self.assertIn("模型=timeout-test-model，图片题=否，可用预算=2.50s", logs)
        self.assertIn("耗时=7.50s，有效预算=7.50s，距模型等待截止=0.00s", logs)
        self.assertTrue(any(values.get("source") == "random_timeout"
                            for kind, values in watcher.hub.events if kind == "problem"))
        self.assertEqual(self.solver.calls, 1)

    async def test_successful_submission_log_distinguishes_receipt_from_correctness(self):
        self.solver.release.set()
        watcher = self.prepare_watcher()
        watcher.cfg["bot"]["dry_run"] = False
        watcher.client.submit_answer = mock.AsyncMock(return_value={"code": 0})

        await self.start_flow(watcher)

        logs = "\n".join(message for _, message in watcher.hub.logs)
        self.assertIn("code=0，服务器接收成功，未验证正确性", logs)
        watcher.client.submit_answer.assert_awaited_once()
        self.assertIn("p", watcher.answered)


if __name__ == "__main__":
    unittest.main()
