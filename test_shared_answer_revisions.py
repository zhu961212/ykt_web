import asyncio
import copy
import unittest
from unittest import mock

from server import SharedAnswerPool


class SharedAnswerRevisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = SharedAnswerPool()
        self.problem = {
            "problem_id": "p1", "type": 1, "title": "Choose one",
            "options": [("A", "first"), ("B", "second")],
        }

    async def asyncTearDown(self):
        await self.pool.clear()

    async def test_changed_content_does_not_reuse_completed_answer(self):
        solver = mock.Mock()
        solver.solve = mock.AsyncMock(side_effect=[
            {"result": ["A"], "display": "A"},
            {"result": ["B"], "display": "B"},
        ])
        first = await self.pool.solve("server", "lesson", "p1", self.problem, solver)
        changed = copy.deepcopy(self.problem)
        changed["options"].reverse()
        second = await self.pool.solve("server", "lesson", "p1", changed, solver)
        self.assertEqual(first["result"], ["A"])
        self.assertEqual(second["result"], ["B"])
        self.assertEqual(solver.solve.await_count, 2)

    async def test_changed_content_does_not_join_inflight_solve(self):
        release = asyncio.Event()
        calls = []

        async def solve(problem):
            calls.append(problem["title"])
            await release.wait()
            return {"result": [problem["title"]], "display": problem["title"]}

        solver = mock.Mock(solve=solve)
        changed = {**self.problem, "title": "Revised"}
        tasks = [asyncio.create_task(self.pool.solve("s", "l", "p1", p, solver))
                 for p in (self.problem, changed)]
        try:
            for _ in range(6):
                await asyncio.sleep(0)
            self.assertCountEqual(calls, ["Choose one", "Revised"])
        finally:
            release.set()
            results = await asyncio.gather(*tasks)
        self.assertNotEqual(results[0], results[1])

    async def test_equivalent_content_still_shares_independent_results(self):
        solver = mock.Mock(solve=mock.AsyncMock(return_value={
            "result": ["A"], "display": "A",
        }))
        first = await self.pool.solve("s", "l", "p1", self.problem, solver)
        first["result"].append("B")
        reordered = dict(reversed(list(self.problem.items())))
        second = await self.pool.solve("s", "l", "p1", reordered, solver)
        self.assertEqual(second["result"], ["A"])
        solver.solve.assert_awaited_once()

    async def test_account_metadata_does_not_duplicate_equivalent_solves(self):
        solver = mock.Mock(solve=mock.AsyncMock(return_value={
            "result": ["A"], "display": "A",
        }))
        first = {**self.problem, "dt": 1, "raw": {"seen_by": "first"}}
        second = {**self.problem, "dt": 2, "raw": {"seen_by": "second"}}
        await self.pool.solve("s", "l", "p1", first, solver)
        result = await self.pool.solve("s", "l", "p1", second, solver)
        self.assertEqual(result["result"], ["A"])
        solver.solve.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
