import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import server
import ykt_core as core
from test_ykt_web import FakeHub, FakeRequest


def _config():
    result = copy.deepcopy(server.DEFAULTS)
    result["llm"].update({
        "base_url": "https://example.invalid/v1",
        "model": "provider-selected-model",
        "api_key": "test-secret",
    })
    return result


class ReasoningEffortPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def solve_payload(self, settings):
        response = mock.MagicMock()
        response.__aenter__ = mock.AsyncMock(return_value=response)
        response.__aexit__ = mock.AsyncMock(return_value=None)
        response.status = 200
        response.json = mock.AsyncMock(return_value={"choices": [{
            "finish_reason": "stop", "message": {"content": '{"answers":["A"]}'},
        }]})
        http = mock.MagicMock()
        http.post.return_value = response
        cfg = _config()
        cfg["llm"] = settings

        solved = await core.LLMSolver(cfg, http).solve({
            "type": 1, "title": "Question", "options": [("A", "One"), ("B", "Two")],
        })

        self.assertEqual(solved["result"], ["A"])
        http.post.assert_called_once()
        return http.post.call_args.kwargs["json"]

    async def test_missing_or_empty_effort_omits_payload_parameter(self):
        for include_empty in (False, True):
            with self.subTest(include_empty=include_empty):
                settings = _config()["llm"]
                if not include_empty:
                    settings.pop("reasoning_effort")

                payload = await self.solve_payload(settings)

                self.assertNotIn("reasoning_effort", payload)

    async def test_selected_effort_is_sent_without_model_name_assumptions(self):
        for effort in ("low", "medium", "high", "xhigh", "max"):
            with self.subTest(effort=effort):
                settings = _config()["llm"]
                settings["reasoning_effort"] = effort

                payload = await self.solve_payload(settings)

                self.assertEqual(payload["reasoning_effort"], effort)
                self.assertEqual(payload["model"], "provider-selected-model")


class ReasoningEffortConfigTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        cfg = _config()
        http = mock.MagicMock()
        solver = core.LLMSolver(cfg, http)
        self.patch = mock.patch.multiple(
            server, cfg=cfg, hub=FakeHub(), account_manager=None, llm_session=http,
            solver=solver, watcher=SimpleNamespace(solver=solver), config_revision=0,
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)

    @staticmethod
    def request(effort, *, include_effort=True):
        llm = {"base_url": server.cfg["llm"]["base_url"],
               "model": server.cfg["llm"]["model"]}
        if include_effort:
            llm["reasoning_effort"] = effort
        return FakeRequest({"llm": llm})

    async def test_default_public_config_preserves_provider_default(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(server, "CONFIG_PATH", Path(directory) / "missing.json"):
                loaded = server.load_config()

        self.assertEqual(loaded["llm"]["reasoning_effort"], "")
        response = await server.api_config_get(FakeRequest())
        payload = json.loads(response.text)
        self.assertEqual(payload["llm"]["reasoning_effort"], "")
        self.assertNotIn("test-secret", response.text)

    async def test_effort_persists_is_preserved_when_omitted_and_can_be_cleared(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            with mock.patch.object(server, "CONFIG_PATH", config_path):
                old_solver = server.solver
                response = await server.api_config(self.request("low"))
                self.assertEqual(json.loads(response.text)["llm"]["reasoning_effort"], "low")
                self.assertEqual(json.loads(config_path.read_text(encoding="utf-8"))["llm"]["reasoning_effort"], "low")
                self.assertEqual(server.solver.cfg["reasoning_effort"], "low")
                self.assertIsNot(server.solver, old_solver)
                self.assertIs(server.watcher.solver, server.solver)

                saved_text = config_path.read_text(encoding="utf-8")
                response = await server.api_config(self.request(None, include_effort=False))
                self.assertEqual(json.loads(response.text)["llm"]["reasoning_effort"], "low")
                self.assertEqual(config_path.read_text(encoding="utf-8"), saved_text)
                self.assertEqual(server.config_revision, 1)

                response = await server.api_config(self.request(""))
                self.assertEqual(json.loads(response.text)["llm"]["reasoning_effort"], "")
                self.assertEqual(json.loads(config_path.read_text(encoding="utf-8"))["llm"]["reasoning_effort"], "")
                self.assertEqual(server.solver.cfg["reasoning_effort"], "")
                self.assertEqual(server.config_revision, 2)

    async def test_every_allowed_effort_is_accepted_by_config_api(self):
        for effort in ("low", "medium", "high", "xhigh", "max"):
            with self.subTest(effort=effort), mock.patch.object(server, "save_config") as save:
                response = await server.api_config(self.request(effort))

                self.assertEqual(json.loads(response.text)["llm"]["reasoning_effort"], effort)
                self.assertEqual(save.call_args.args[0]["llm"]["reasoning_effort"], effort)

    async def test_switching_endpoint_or_model_clears_omitted_effort(self):
        changes = ({"model": "different-provider-model"},
                   {"base_url": "https://different.example.invalid/v1", "api_key": "replacement-secret"})
        for change in changes:
            with self.subTest(change=change), mock.patch.object(server, "save_config"):
                await server.api_config(self.request("high"))
                request = self.request(None, include_effort=False)
                request.body["llm"].update(change)

                response = await server.api_config(request)

                self.assertEqual(json.loads(response.text)["llm"]["reasoning_effort"], "")
                self.assertEqual(server.solver.cfg["reasoning_effort"], "")

    async def test_switching_model_preserves_explicit_effort_choice(self):
        request = self.request("medium")
        request.body["llm"]["model"] = "different-provider-model"
        with mock.patch.object(server, "save_config"):
            response = await server.api_config(request)

        self.assertEqual(json.loads(response.text)["llm"]["reasoning_effort"], "medium")

    async def test_invalid_effort_does_not_write_or_change_runtime(self):
        for effort in (None, False, True, 0, 1, [], {}, "none", "minimal", "LOW", " low ", "ultra", "high\n"):
            with self.subTest(effort=effort):
                before = copy.deepcopy(server.cfg)
                old_solver = server.solver
                request = self.request(effort)
                request.body["auto_answer"] = True
                with mock.patch.object(server, "save_config") as save:
                    with self.assertRaisesRegex(ValueError, "reasoning_effort"):
                        await server.api_config(request)

                save.assert_not_called()
                self.assertEqual(server.cfg, before)
                self.assertIs(server.solver, old_solver)
                self.assertIs(server.watcher.solver, old_solver)
                self.assertEqual(server.config_revision, 0)


if __name__ == "__main__":
    unittest.main()
