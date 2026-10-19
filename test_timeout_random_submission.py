import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

import aiohttp

import server
import ykt_core as core
from test_deadline_extensions import _Hub
from test_request_deadline import _Clock


class TimeoutRandomSubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = _Clock()
        patch = mock.patch.object(server, "time", new=self.clock)
        patch.start()
        self.addCleanup(patch.stop)
        self.pool = server.SharedAnswerPool()
        self.watchers = []

    async def asyncTearDown(self):
        for watcher in self.watchers:
            await watcher.stop()
        await self.pool.clear()

    def prepare_watcher(self, *, problem_id="p", problem_type=1, dry_run=False):
        started = asyncio.Event()

        async def pending_solver(_problem):
            started.set()
            await asyncio.Event().wait()

        solver = SimpleNamespace(solve=mock.AsyncMock(side_effect=pending_solver), started=started)
        client = SimpleNamespace(
            server_key="test", bearer="", lesson_token="",
            submit_answer=mock.AsyncMock(return_value={"code": 0}),
        )
        watcher = server.Watcher(
            {"bot": {"dry_run": dry_run, "answer_delay_seconds": 0,
                     "request_margin_seconds": 1.5},
             "llm": {"model": "test", "vision_enabled": True}},
            client, solver, _Hub(), self.pool,
        )
        watcher.running = True
        watcher.lesson_id = "lesson"
        watcher.problems[problem_id] = {
            "problem_id": problem_id, "type": problem_type, "title": "Question",
            "options": [("A", "One"), ("B", "Two"), ("C", "Three")],
        }
        watcher.question_windows[problem_id] = watcher._question_window(
            int(self.clock.start * 1000), 45, self.clock.now,
        )
        self.watchers.append(watcher)
        return watcher

    @staticmethod
    def start_flow(watcher, problem_id="p"):
        task = asyncio.create_task(watcher._answer_flow(problem_id))
        watcher.answer_tasks[problem_id] = task
        return task

    async def run_expired_solve(self, watcher, problem_id="p"):
        original_wait_for = asyncio.wait_for

        async def expire_wait(tasks, *, timeout):
            await watcher.solver.started.wait()
            self.clock.now = watcher.question_windows[problem_id]["submit_by"]
            return set(), set(tasks)

        with mock.patch.object(server.asyncio, "wait", new=expire_wait):
            await original_wait_for(self.start_flow(watcher, problem_id), timeout=1)

    def assert_random_result(self, watcher, *, status="submitted"):
        final_events = [values for kind, values in watcher.hub.events
                        if kind == "problem" and values.get("status") == status]
        self.assertTrue(final_events)
        self.assertEqual(final_events[-1].get("source"), "random_timeout")
        self.assertTrue(any("random_timeout" in message for _, message in watcher.hub.logs))

    async def test_soft_timeout_submits_valid_random_choice_without_legacy_fallback(self):
        watcher = self.prepare_watcher()
        with mock.patch.object(core, "fallback_result", side_effect=AssertionError("legacy fallback used")) as old:
            await self.run_expired_solve(watcher)

        old.assert_not_called()
        watcher.client.submit_answer.assert_awaited_once()
        watcher.solver.solve.assert_awaited_once()
        self.assertIn(watcher.client.submit_answer.await_args.args[3], (["A"], ["B"], ["C"]))
        self.assertEqual(watcher.client.submit_answer.await_args.kwargs["timeout"], 1.5)
        self.assert_random_result(watcher)

    async def test_random_fallback_can_dispatch_after_soft_cutoff_before_hard_cutoff(self):
        watcher = self.prepare_watcher()
        original_wait_for = asyncio.wait_for

        async def delayed_dispatch(awaitable, *, timeout):
            self.clock.now += 0.2
            return await original_wait_for(awaitable, timeout=timeout)

        with mock.patch.object(server.asyncio, "wait_for", new=delayed_dispatch):
            await self.run_expired_solve(watcher)

        watcher.client.submit_answer.assert_awaited_once()
        self.assertAlmostEqual(watcher.client.submit_answer.await_args.kwargs["timeout"], 1.3, places=5)
        self.assert_random_result(watcher)

    async def test_accounts_share_random_result_and_do_not_restart_original_llm(self):
        first = self.prepare_watcher()
        second = self.prepare_watcher()
        both_waiting = asyncio.Event()
        solver_started = asyncio.Event()
        waits = []
        original_wait_for = asyncio.wait_for

        async def pending_shared_solver(_problem):
            solver_started.set()
            await asyncio.Event().wait()

        first.solver.solve.side_effect = pending_shared_solver
        second.solver.solve.side_effect = pending_shared_solver

        async def expire_both(tasks, *, timeout):
            waits.append(timeout)
            if len(waits) == 2:
                self.clock.now = first.question_windows["p"]["submit_by"]
                both_waiting.set()
            await both_waiting.wait()
            await solver_started.wait()
            return set(), set(tasks)

        with mock.patch.object(server.asyncio, "wait", new=expire_both):
            await original_wait_for(asyncio.gather(self.start_flow(first), self.start_flow(second)), timeout=1)

        self.assertEqual(first.solver.solve.await_count + second.solver.solve.await_count, 1)
        first.client.submit_answer.assert_awaited_once()
        second.client.submit_answer.assert_awaited_once()
        self.assertEqual(first.client.submit_answer.await_args.args[3], second.client.submit_answer.await_args.args[3])
        self.assert_random_result(first)
        self.assert_random_result(second)

    async def test_late_account_reuses_model_answer_completed_before_soft_deadline(self):
        first = self.prepare_watcher()
        first.solver.solve.side_effect = None
        first.solver.solve.return_value = {"result": ["A"], "display": "A"}
        await self.start_flow(first)
        self.assertEqual(first.client.submit_answer.await_args.args[3], ["A"])
        self.clock.now = self.clock.start + 43.6
        second = self.prepare_watcher()

        with mock.patch.object(core, "random_choice_result", side_effect=AssertionError("timely answer was replaced")) as random:
            await self.start_flow(second)

        random.assert_not_called()
        first.solver.solve.assert_awaited_once()
        second.solver.solve.assert_not_awaited()
        self.assertEqual(second.client.submit_answer.await_args.args[3], ["A"])
        self.assertIn("p", second.answered)
        self.assertFalse(any(values.get("source") == "random_timeout"
                             for kind, values in second.hub.events if kind == "problem"))

    async def test_original_waiters_receive_pinned_random_when_late_model_raises(self):
        watcher = self.prepare_watcher()
        problem = watcher.problems["p"]
        started = asyncio.Event()
        release = asyncio.Event()

        async def fail_after_random_is_pinned(_problem):
            started.set()
            await release.wait()
            raise RuntimeError("late provider failure")

        watcher.solver.solve.side_effect = fail_after_random_is_pinned
        first = asyncio.create_task(self.pool.solve("test", "lesson", "p", problem, watcher.solver))
        second = asyncio.create_task(self.pool.solve("test", "lesson", "p", problem, watcher.solver))
        await asyncio.wait_for(started.wait(), timeout=1)
        self.clock.now = watcher.question_windows["p"]["submit_by"]
        pinned = await self.pool.random_fallback(
            "test", "lesson", "p", problem, model_deadline=self.clock.now,
        )
        release.set()

        results = await asyncio.wait_for(asyncio.gather(first, second), timeout=1)

        self.assertEqual(pinned["source"], "random_timeout")
        self.assertEqual(results, [pinned, pinned])
        watcher.solver.solve.assert_awaited_once()
        self.assertEqual(self.pool._inflight, {})

    async def test_completion_timestamps_follow_model_and_random_eviction_and_clear(self):
        pool = server.SharedAnswerPool(max_completed=1)
        self.addAsyncCleanup(pool.clear)
        solver = SimpleNamespace(solve=mock.AsyncMock(return_value={"result": ["A"], "display": "A"}))
        problem = {"type": 1, "title": "Question", "options": [("A", "One"), ("B", "Two")]}
        first_key = pool._key("test", "lesson", "first", problem)
        random_key = pool._key("test", "lesson", "random", problem)
        last_key = pool._key("test", "lesson", "last", problem)
        await pool.solve("test", "lesson", "first", problem, solver)
        self.assertEqual(pool._completed_at, {first_key: self.clock.now})
        self.clock.now += 1

        await pool.random_fallback("test", "lesson", "random", problem, model_deadline=self.clock.now)

        self.assertEqual(set(pool._completed), {random_key})
        self.assertEqual(pool._completed_at, {random_key: self.clock.now})
        self.clock.now += 1
        await pool.solve("test", "lesson", "last", problem, solver)
        self.assertEqual(set(pool._completed), {last_key})
        self.assertEqual(pool._completed_at, {last_key: self.clock.now})

        await pool.clear()

        self.assertEqual(pool._completed, {})
        self.assertEqual(pool._completed_at, {})

    async def test_random_fallback_dispatch_at_real_deadline_never_posts(self):
        watcher = self.prepare_watcher()
        original_wait_for = asyncio.wait_for

        async def dispatch_at_hard_cutoff(awaitable, *, timeout):
            self.clock.now = watcher.question_windows["p"]["closes_at"]
            return await original_wait_for(awaitable, timeout=timeout)

        with mock.patch.object(server.asyncio, "wait_for", new=dispatch_at_hard_cutoff):
            await self.run_expired_solve(watcher)

        watcher.client.submit_answer.assert_not_awaited()
        self.assertEqual(watcher.submission_attempts, set())
        self.assertNotIn("p", watcher.answered)

    async def test_late_model_choice_is_replaced_by_shared_random_source(self):
        for index, offset in enumerate((0, 0.2)):
            with self.subTest(offset=offset):
                self.clock.now = self.clock.start
                problem_id = f"late-choice-{index}"
                watcher = self.prepare_watcher(problem_id=problem_id)

                async def late_choice(_problem):
                    self.clock.now = watcher.question_windows[problem_id]["submit_by"] + offset
                    return {"result": ["A"], "display": "A"}

                watcher.solver.solve.side_effect = late_choice
                with mock.patch.object(self.pool, "random_fallback", new=mock.AsyncMock(return_value={
                    "result": ["B"], "display": "B", "source": "random_timeout",
                })) as fallback:
                    await self.start_flow(watcher, problem_id)

                fallback.assert_awaited_once()
                self.assertEqual(watcher.client.submit_answer.await_args.args[3], ["B"])
                self.assert_random_result(watcher)

    async def test_event_first_received_after_soft_cutoff_can_fallback_from_cached_problem(self):
        watcher = self.prepare_watcher()
        self.clock.now = watcher.question_windows["p"]["submit_by"] + 0.2
        watcher._schedule_problem({"prob": "p", "dt": int(self.clock.start * 1000), "limit": 45})

        await watcher.answer_tasks["p"]

        watcher.solver.solve.assert_not_awaited()
        watcher.client.submit_answer.assert_awaited_once()
        self.assert_random_result(watcher)

    async def test_no_result_can_start_post_at_or_after_true_deadline(self):
        for index, offset in enumerate((0, 0.01)):
            with self.subTest(offset=offset):
                self.clock.now = self.clock.start
                problem_id = f"hard-expired-{index}"
                watcher = self.prepare_watcher(problem_id=problem_id)

                async def answer_after_hard_deadline(_problem):
                    self.clock.now = watcher.question_windows[problem_id]["closes_at"] + offset
                    return {"result": ["A"], "display": "A"}

                watcher.solver.solve.side_effect = answer_after_hard_deadline
                await self.start_flow(watcher, problem_id)

                watcher.client.submit_answer.assert_not_awaited()
                self.assertNotIn(problem_id, watcher.answered)
                self.assertEqual(watcher.submission_attempts, set())

    async def test_non_choice_parse_error_and_missing_options_never_get_random_submissions(self):
        cases = (
            {"type": 4}, {"type": 5}, {"parse_error": "question parsing failed"}, {"options": []},
        )
        for index, changes in enumerate(cases):
            with self.subTest(changes=changes):
                self.clock.now = self.clock.start
                problem_id = f"unsupported-{index}"
                watcher = self.prepare_watcher(problem_id=problem_id)
                watcher.problems[problem_id].update(changes)

                await self.run_expired_solve(watcher, problem_id)

                watcher.client.submit_answer.assert_not_awaited()
                self.assertNotIn(problem_id, watcher.answered)

    async def test_early_model_json_image_and_provider_timeout_errors_do_not_fallback(self):
        failures = (
            RuntimeError("model service unavailable"), ValueError("invalid answer JSON"),
            RuntimeError("image download failed"), asyncio.TimeoutError("provider timeout"), None,
        )
        for index, failure in enumerate(failures):
            with self.subTest(failure=failure):
                problem_id = f"early-error-{index}"
                watcher = self.prepare_watcher(problem_id=problem_id)
                watcher.solver.solve.side_effect = failure
                watcher.solver.solve.return_value = None
                with mock.patch.object(self.pool, "random_fallback", new=mock.AsyncMock()) as fallback:
                    await self.start_flow(watcher, problem_id)

                fallback.assert_not_awaited()
                watcher.client.submit_answer.assert_not_awaited()
                self.assertFalse(any(values.get("source") == "random_timeout"
                                     for kind, values in watcher.hub.events if kind == "problem"))

    async def test_dry_run_previews_random_answer_without_upstream_post(self):
        watcher = self.prepare_watcher(dry_run=True)

        await self.run_expired_solve(watcher)

        watcher.client.submit_answer.assert_not_awaited()
        self.assert_random_result(watcher, status="dry")

    async def test_uncertain_random_submission_stays_blocked_on_changed_timestamp(self):
        watcher = self.prepare_watcher()
        watcher.client.submit_answer.side_effect = aiohttp.ClientConnectionError("connection lost")

        await self.run_expired_solve(watcher)

        self.assert_random_result(watcher, status="uncertain(network)")
        self.assertNotIn("p", watcher.answered)
        self.assertEqual(watcher.uncertain_submissions, {"p"})
        watcher._schedule_problem({
            "prob": "p", "dt": int((self.clock.start - 0.5) * 1000), "limit": 45,
        })
        self.assertNotIn("p", watcher.answer_tasks)
        watcher.client.submit_answer.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
