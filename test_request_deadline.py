import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

import aiohttp

import server
from test_deadline_extensions import _Hub


class _Clock:
    def __init__(self):
        self.start = 1_700_000_000.0
        self.now = self.start

    def time(self):
        return self.now

    def monotonic(self):
        return 100 + self.now - self.start


class RequestDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = _Clock()
        self.clock_patch = mock.patch.object(server, "time", new=self.clock)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.pool = server.SharedAnswerPool()
        self.watchers = []

    async def asyncTearDown(self):
        for watcher in self.watchers:
            await watcher.stop()
        await self.pool.clear()

    def prepare_watcher(self, *, limit=45, answer_delay=0, problem_id="p"):
        solver = SimpleNamespace(solve=mock.AsyncMock(return_value={
            "result": ["A"], "display": "A",
        }))
        client = SimpleNamespace(
            server_key="test", bearer="", lesson_token="",
            submit_answer=mock.AsyncMock(return_value={"code": 0}),
        )
        watcher = server.Watcher(
            {"bot": {"dry_run": False, "answer_delay_seconds": answer_delay,
                     "safety_seconds": 8, "request_margin_seconds": 1.5},
             "llm": {"model": "test-model"}},
            client, solver, _Hub(), self.pool,
        )
        watcher.running = True
        watcher.lesson_id = "lesson"
        watcher.problems[problem_id] = {
            "problem_id": problem_id, "type": 1, "title": "Question",
            "options": [("A", "One"), ("B", "Two")],
        }
        watcher.question_windows[problem_id] = watcher._question_window(
            int(self.clock.start * 1000), limit, self.clock.now,
        )
        self.watchers.append(watcher)
        return watcher

    @staticmethod
    def start_flow(watcher, problem_id="p"):
        task = asyncio.create_task(watcher._answer_flow(problem_id))
        watcher.answer_tasks[problem_id] = task
        return task

    @staticmethod
    def statuses(watcher):
        return [values.get("status") for kind, values in watcher.hub.events if kind == "problem"]

    def assert_uncertain(self, watcher, status):
        self.assertIn(status, self.statuses(watcher))
        self.assertNotIn("p", watcher.answered)
        messages = "\n".join(message for _, message in watcher.hub.logs)
        self.assertIn("提交结果未确认", messages)
        self.assertIn("未自动重发", messages)
        self.assertNotIn("服务器接收成功", messages)

    async def test_45_second_problem_ignores_legacy_safety_and_reserves_margin_once(self):
        watcher = self.prepare_watcher()
        for legacy_safety in (0, 8, 999):
            with self.subTest(legacy_safety=legacy_safety):
                watcher.cfg["bot"]["safety_seconds"] = legacy_safety
                window = watcher._question_window(int(self.clock.start * 1000), 45, self.clock.now)
                self.assertEqual(window["closes_at"], self.clock.start + 45)
                self.assertEqual(window["submit_by"], self.clock.start + 43.5)

        await self.start_flow(watcher)

        messages = "\n".join(message for _, message in watcher.hub.logs)
        self.assertIn("可用预算=43.50s", messages)
        self.assertEqual(watcher.client.submit_answer.await_args.kwargs["timeout"], 45)

    async def test_delayed_event_preserves_original_deadline_and_remaining_budget(self):
        self.clock.now += 30
        watcher = self.prepare_watcher()
        watcher._schedule_problem({"prob": "p", "dt": int(self.clock.start * 1000), "limit": 45})

        await watcher.answer_tasks["p"]

        window = watcher.question_windows["p"]
        self.assertEqual(window["closes_at"], self.clock.start + 45)
        self.assertEqual(window["submit_by"], self.clock.start + 43.5)
        self.assertEqual(watcher.client.submit_answer.await_args.kwargs["timeout"], 15)
        self.assertEqual(watcher.client.submit_answer.await_args.args[1], int(self.clock.start * 1000))
        self.assertTrue(any("可用预算=13.50s" in message for _, message in watcher.hub.logs))

    async def test_choice_model_returning_at_or_after_soft_deadline_uses_random_source(self):
        for index, offset in enumerate((0, 0.001)):
            with self.subTest(offset=offset):
                problem_id = f"boundary-{index}"
                self.clock.now = self.clock.start
                watcher = self.prepare_watcher(problem_id=problem_id)

                async def finish_at_boundary(_problem):
                    self.clock.now = watcher.question_windows[problem_id]["submit_by"] + offset
                    return {"result": ["A"], "display": "A"}

                watcher.solver.solve.side_effect = finish_at_boundary
                await self.start_flow(watcher, problem_id)

                watcher.client.submit_answer.assert_awaited_once()
                self.assertIn(problem_id, watcher.answered)
                self.assertIn("submitted", self.statuses(watcher))
                self.assertTrue(any(values.get("source") == "random_timeout"
                                    for kind, values in watcher.hub.events if kind == "problem"))

    async def test_answer_just_before_send_deadline_uses_entire_http_remainder(self):
        watcher = self.prepare_watcher()
        original_wait_for = asyncio.wait_for

        async def finish_just_in_time(_problem):
            self.clock.now = watcher.question_windows["p"]["submit_by"] - 0.001
            return {"result": ["A"], "display": "A"}

        watcher.solver.solve.side_effect = finish_just_in_time
        with mock.patch.object(server.asyncio, "wait_for", wraps=original_wait_for) as hard_timeout:
            await self.start_flow(watcher)

        self.assertAlmostEqual(watcher.client.submit_answer.await_args.kwargs["timeout"], 1.501, places=5)
        self.assertAlmostEqual(hard_timeout.call_args.kwargs["timeout"], 1.501, places=5)
        self.assertIn("p", watcher.answered)

    async def test_dispatch_scheduled_at_or_after_real_deadline_never_posts(self):
        original_wait_for = asyncio.wait_for
        for index, offset in enumerate((0, 0.001)):
            with self.subTest(offset=offset):
                self.clock.now = self.clock.start
                problem_id = f"dispatch-expired-{index}"
                watcher = self.prepare_watcher(problem_id=problem_id)

                async def dispatch_after_cutoff(awaitable, *, timeout):
                    self.clock.now = watcher.question_windows[problem_id]["closes_at"] + offset
                    return await original_wait_for(awaitable, timeout=timeout)

                with mock.patch.object(server.asyncio, "wait_for", new=dispatch_after_cutoff):
                    await self.start_flow(watcher, problem_id)

                watcher.client.submit_answer.assert_not_awaited()
                self.assertEqual(watcher.submission_attempts, set())
                self.assertNotIn(problem_id, watcher.answered)
                self.assertIn("expired", self.statuses(watcher))

    async def test_ready_answer_may_dispatch_after_soft_deadline_before_real_deadline(self):
        watcher = self.prepare_watcher()
        original_wait_for = asyncio.wait_for

        async def dispatch_after_model_cutoff(awaitable, *, timeout):
            self.clock.now = watcher.question_windows["p"]["submit_by"] + 0.2
            return await original_wait_for(awaitable, timeout=timeout)

        with mock.patch.object(server.asyncio, "wait_for", new=dispatch_after_model_cutoff):
            await self.start_flow(watcher)

        watcher.client.submit_answer.assert_awaited_once()
        self.assertAlmostEqual(watcher.client.submit_answer.await_args.kwargs["timeout"], 1.3, places=5)
        self.assertIn("p", watcher.answered)
        self.assertFalse(any(values.get("source") == "random_timeout"
                             for kind, values in watcher.hub.events if kind == "problem"))

    async def test_dispatch_recomputes_http_timeout_after_scheduling_delay(self):
        watcher = self.prepare_watcher()
        original_wait_for = asyncio.wait_for

        async def dispatch_one_second_later(awaitable, *, timeout):
            self.clock.now += 1
            return await original_wait_for(awaitable, timeout=timeout)

        with mock.patch.object(server.asyncio, "wait_for", new=dispatch_one_second_later):
            await self.start_flow(watcher)

        self.assertEqual(watcher.client.submit_answer.await_args.kwargs["timeout"], 44)
        self.assertIn("p", watcher.answered)

    async def test_short_question_skips_delay_that_reaches_send_deadline(self):
        for index, delay in enumerate((1.5, 5)):
            with self.subTest(delay=delay):
                problem_id = f"short-{index}"
                watcher = self.prepare_watcher(limit=3, answer_delay=delay, problem_id=problem_id)
                with mock.patch.object(server.asyncio, "sleep", new=mock.AsyncMock()) as sleep:
                    await self.start_flow(watcher, problem_id)

                sleep.assert_not_awaited()
                watcher.client.submit_answer.assert_awaited_once()
                self.assertIn(problem_id, watcher.answered)

    async def test_no_answer_at_soft_deadline_posts_random_answer_with_request_margin(self):
        watcher = self.prepare_watcher()
        solver_started = asyncio.Event()

        async def blocked_solver(_problem):
            solver_started.set()
            await asyncio.Event().wait()

        async def expire_wait(tasks, *, timeout):
            await solver_started.wait()
            self.clock.now += timeout
            return set(), set(tasks)

        watcher.solver.solve.side_effect = blocked_solver
        with mock.patch.object(server.asyncio, "wait", new=expire_wait):
            await self.start_flow(watcher)

        self.assertEqual(self.clock.now, watcher.question_windows["p"]["submit_by"])
        watcher.client.submit_answer.assert_awaited_once()
        self.assertEqual(watcher.client.submit_answer.await_args.kwargs["timeout"], 1.5)
        self.assertIn("submitted", self.statuses(watcher))
        self.assertIn("p", watcher.answered)
        self.assertTrue(any(values.get("source") == "random_timeout"
                            for kind, values in watcher.hub.events if kind == "problem"))

    async def test_hanging_http_is_cancelled_by_outer_hard_deadline(self):
        watcher = self.prepare_watcher()
        request_cancelled = asyncio.Event()
        original_wait_for = asyncio.wait_for
        observed_timeouts = []

        async def ignored_client_timeout(*_args, **_kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                request_cancelled.set()

        async def fast_hard_timeout(awaitable, *, timeout):
            observed_timeouts.append(timeout)
            return await original_wait_for(awaitable, timeout=0.02)

        watcher.client.submit_answer.side_effect = ignored_client_timeout
        with mock.patch.object(server.asyncio, "wait_for", new=fast_hard_timeout):
            await original_wait_for(self.start_flow(watcher), timeout=1)

        self.assertEqual(observed_timeouts, [45])
        self.assertTrue(request_cancelled.is_set())
        self.assert_uncertain(watcher, "uncertain(timeout)")
        watcher.client.submit_answer.assert_awaited_once()

    async def test_response_at_or_after_real_deadline_is_not_counted_as_success(self):
        for index, offset in enumerate((0, 0.001)):
            with self.subTest(offset=offset):
                problem_id = f"late-{index}"
                self.clock.now = self.clock.start
                watcher = self.prepare_watcher(problem_id=problem_id)

                async def late_response(*_args, **_kwargs):
                    self.clock.now = watcher.question_windows[problem_id]["closes_at"] + offset
                    return {"code": 0}

                watcher.client.submit_answer.side_effect = late_response
                await self.start_flow(watcher, problem_id)

                self.assert_uncertain(watcher, "uncertain(timeout)")
                self.assertNotIn(problem_id, watcher.answered)

    async def test_early_network_error_then_replay_never_reposts_same_attempt(self):
        watcher = self.prepare_watcher()
        watcher.client.submit_answer.side_effect = aiohttp.ClientConnectionError("connection lost")
        event = {"prob": "p", "dt": int(self.clock.start * 1000), "limit": 45}
        watcher._schedule_problem(event)
        await watcher.answer_tasks["p"]
        self.assert_uncertain(watcher, "uncertain(network)")

        watcher._schedule_problem(event)
        self.assertNotIn("p", watcher.answer_tasks)
        await watcher._answer_flow("p")

        watcher.client.submit_answer.assert_awaited_once()
        self.assertIn(("p", int(self.clock.start * 1000)), watcher.submission_attempts)

    async def test_replayed_events_with_unusable_timestamp_preserve_window_and_attempt(self):
        missing = object()
        invalid_timestamps = (
            missing, None, "not-a-timestamp",
            self.clock.start - 172_800, self.clock.start + 172_800,
        )
        for index, dt in enumerate(invalid_timestamps):
            with self.subTest(dt="missing" if dt is missing else dt):
                self.clock.now = self.clock.start
                problem_id = f"invalid-time-{index}"
                watcher = self.prepare_watcher(problem_id=problem_id)
                watcher.question_windows.clear()
                watcher.client.submit_answer.return_value = {"code": 4}
                event = {"prob": problem_id, "limit": 45}
                if dt is not missing:
                    event["dt"] = dt
                watcher._schedule_problem(event)
                await watcher.answer_tasks[problem_id]
                first_window = watcher.question_windows[problem_id]
                first_snapshot = dict(first_window)
                self.assertFalse(first_window["server_dt_valid"])

                self.clock.now += 5
                watcher._schedule_problem(event)
                replay_task = watcher.answer_tasks.get(problem_id)
                if replay_task is not None:
                    await replay_task

                self.assertIsNone(replay_task)
                self.assertIs(watcher.question_windows[problem_id], first_window)
                self.assertEqual(watcher.question_windows[problem_id], first_snapshot)
                watcher.client.submit_answer.assert_awaited_once()
                self.assertEqual(watcher.submission_attempts, {(problem_id, first_window["dt_ms"])})

    async def test_uncertain_submission_blocks_same_question_when_server_timestamp_changes(self):
        watcher = self.prepare_watcher()
        watcher.question_windows.clear()
        watcher.client.submit_answer.side_effect = asyncio.TimeoutError("response not confirmed")
        watcher._schedule_problem({"prob": "p", "limit": 45})
        await watcher.answer_tasks["p"]
        self.assert_uncertain(watcher, "uncertain(timeout)")
        self.assertEqual(watcher.uncertain_submissions, {"p"})
        first_window = watcher.question_windows["p"]
        self.assertFalse(first_window["server_dt_valid"])
        self.clock.now += 5

        for server_start in (self.clock.start - 0.5, self.clock.start + 2):
            with self.subTest(server_start=server_start):
                watcher._schedule_problem({
                    "prob": "p", "limit": 45, "dt": int(server_start * 1000),
                })
                replay_task = watcher.answer_tasks.get("p")
                if replay_task is not None:
                    await replay_task

                self.assertIsNone(replay_task)
                self.assertIs(watcher.question_windows["p"], first_window)
                watcher.client.submit_answer.assert_awaited_once()
                self.assertEqual(watcher.uncertain_submissions, {"p"})

    async def test_presentation_and_solver_share_one_absolute_send_deadline(self):
        watcher = self.prepare_watcher()
        problem = watcher.problems.pop("p")
        watcher.question_windows["p"]["presentation_id"] = "pres"
        original_wait = asyncio.wait
        budgets = []

        async def load_presentation(*_args, **_kwargs):
            self.clock.now += 10
            watcher.problems["p"] = problem
            return True

        async def observe_wait(tasks, *, timeout):
            budgets.append(timeout)
            return await original_wait(tasks, timeout=1)

        watcher._load_presentation = mock.AsyncMock(side_effect=load_presentation)
        with mock.patch.object(server.asyncio, "wait", new=observe_wait):
            await self.start_flow(watcher)

        self.assertEqual(budgets, [43.5, 33.5])
        self.assertEqual(watcher.client.submit_answer.await_args.kwargs["timeout"], 35)
        self.assertIn("p", watcher.answered)

    async def test_invalid_receipt_blocks_replay_even_when_timestamp_changes(self):
        for index, response in enumerate((None, [], {}, {"code": None}, {"code": False}, {"code": "0"})):
            with self.subTest(response=response):
                self.clock.now = self.clock.start
                problem_id = f"bad-receipt-{index}"
                watcher = self.prepare_watcher(problem_id=problem_id)
                watcher.client.submit_answer.return_value = response
                event = {"prob": problem_id, "limit": 45}
                watcher._schedule_problem(event)
                await watcher.answer_tasks[problem_id]
                self.assertIn("uncertain(response)", self.statuses(watcher))
                self.assertIn(problem_id, watcher.uncertain_submissions)
                self.assertNotIn(problem_id, watcher.answered)

                self.clock.now += 1
                event["dt"] = int((self.clock.start + 0.5) * 1000)
                watcher._schedule_problem(event)
                await watcher._answer_flow(problem_id)

                self.assertNotIn(problem_id, watcher.answer_tasks)
                watcher.client.submit_answer.assert_awaited_once()

    async def test_class_cleanup_removes_attempt_and_uncertain_markers(self):
        watcher = self.prepare_watcher()
        watcher.client.submit_answer.side_effect = asyncio.TimeoutError("response not confirmed")
        await self.start_flow(watcher)
        self.assertTrue(watcher.submission_attempts)
        self.assertEqual(watcher.uncertain_submissions, {"p"})

        watcher._clear_class()

        self.assertEqual(watcher.submission_attempts, set())
        self.assertEqual(watcher.uncertain_submissions, set())


if __name__ == "__main__":
    unittest.main()
