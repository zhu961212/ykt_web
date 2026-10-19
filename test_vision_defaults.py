import copy
import json
import tempfile
import time
import unittest
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import server
import ykt_core as core
from test_ykt_web import FakeHub, FakeRequest


def _config():
    value = copy.deepcopy(server.DEFAULTS)
    value["llm"].update({
        "base_url": "https://example.invalid/v1",
        "api_key": "test-secret",
        "model": "image-model",
    })
    value["bot"]["answer_delay_seconds"] = 0
    return value


class VisionDefaultsTests(unittest.TestCase):
    def test_missing_configuration_enables_images(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(server, "CONFIG_PATH", Path(directory) / "missing.json"):
                loaded = server.load_config()

        self.assertTrue(loaded["llm"]["vision_enabled"])
        self.assertTrue(server.public_llm_config({"llm": {}})["vision_enabled"])

    def test_legacy_config_preserves_explicit_false_and_defaults_missing_flag_to_true(self):
        for settings, expected in (({}, True), ({"vision_enabled": False}, False),
                                   ({"vision_enabled": True}, True)):
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                path.write_text(json.dumps({"llm": settings}), encoding="utf-8")
                with mock.patch.object(server, "CONFIG_PATH", path):
                    loaded = server.load_config()

            self.assertIs(loaded["llm"]["vision_enabled"], expected)
            self.assertIs(server.public_llm_config(loaded)["vision_enabled"], expected)

    def test_example_config_enables_images(self):
        value = json.loads((Path(__file__).parent / "config.example.json").read_text(encoding="utf-8"))
        self.assertTrue(value["llm"]["vision_enabled"])


class VisionApiDefaultsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        cfg = _config()
        cfg["llm"]["vision_enabled"] = False
        http = mock.MagicMock()
        solver = core.LLMSolver(cfg, http)
        patch = mock.patch.multiple(
            server, cfg=cfg, hub=FakeHub(), account_manager=None, llm_session=http,
            solver=solver, watcher=SimpleNamespace(solver=solver), config_revision=0,
        )
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def request(**changes):
        llm = {"base_url": server.cfg["llm"]["base_url"],
               "model": server.cfg["llm"]["model"]}
        llm.update(changes)
        return FakeRequest({"llm": llm})

    async def test_model_switch_without_explicit_vision_defaults_to_true(self):
        with mock.patch.object(server, "save_config") as save:
            response = await server.api_config(self.request(model="next-image-model"))

        self.assertTrue(json.loads(response.text)["llm"]["vision_enabled"])
        self.assertTrue(save.call_args.args[0]["llm"]["vision_enabled"])
        self.assertTrue(server.solver.cfg["vision_enabled"])

    async def test_api_switch_without_explicit_vision_defaults_to_true(self):
        with mock.patch.object(server, "save_config") as save:
            response = await server.api_config(self.request(
                base_url="https://different.example.invalid/v1", api_key="replacement-key",
            ))

        self.assertTrue(json.loads(response.text)["llm"]["vision_enabled"])
        self.assertTrue(save.call_args.args[0]["llm"]["vision_enabled"])

    async def test_unchanged_model_preserves_explicit_false_when_flag_is_omitted(self):
        with mock.patch.object(server, "save_config") as save:
            response = await server.api_config(self.request())

        self.assertFalse(json.loads(response.text)["llm"]["vision_enabled"])
        self.assertFalse(server.cfg["llm"]["vision_enabled"])
        save.assert_not_called()

    async def test_switch_can_explicitly_keep_images_disabled(self):
        with mock.patch.object(server, "save_config") as save:
            response = await server.api_config(self.request(
                model="text-only-model", vision_enabled=False,
            ))

        self.assertFalse(json.loads(response.text)["llm"]["vision_enabled"])
        self.assertFalse(save.call_args.args[0]["llm"]["vision_enabled"])
        self.assertFalse(server.solver.cfg["vision_enabled"])

    async def test_manual_false_is_saved_and_survives_reload(self):
        server.cfg["llm"]["vision_enabled"] = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with mock.patch.object(server, "CONFIG_PATH", path):
                response = await server.api_config(self.request(vision_enabled=False))
                restored = server.load_config()

        self.assertFalse(json.loads(response.text)["llm"]["vision_enabled"])
        self.assertFalse(restored["llm"]["vision_enabled"])
        self.assertFalse(server.solver.cfg["vision_enabled"])


class VisionRuntimeDefaultsTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_vision_flag_allows_images_through_watcher_and_solver(self):
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
        cfg["llm"].pop("vision_enabled")
        solver = core.LLMSolver(cfg, http)
        client = SimpleNamespace(server_key="test", bearer="", lesson_token="")
        watcher = server.Watcher(cfg, client, solver, FakeHub())
        watcher.running = True
        watcher.lesson_id = "lesson"
        image_url = "https://cdn.example.edu/question.png"
        watcher.problems["p"] = {
            "problem_id": "p", "type": 1, "title": "Choose from the picture",
            "options": [("A", "One"), ("B", "Two")], "images": [image_url],
        }
        try:
            watcher._schedule_problem({"prob": "p", "dt": int(time.time() * 1000), "limit": 30})
            await watcher.answer_tasks["p"]

            self.assertIn("p", watcher.answered)
            http.post.assert_called_once()
            content = http.post.call_args.kwargs["json"]["messages"][1]["content"]
            self.assertIn({"type": "image_url", "image_url": {"url": image_url}}, content)
        finally:
            await watcher.stop()

    async def test_explicit_false_still_blocks_image_request(self):
        cfg = _config()
        cfg["llm"]["vision_enabled"] = False
        http = mock.MagicMock()

        with self.assertRaisesRegex(RuntimeError, "图片"):
            await core.LLMSolver(cfg, http).solve({
                "type": 1, "title": "Choose from the picture",
                "options": [("A", "One"), ("B", "Two")],
                "images": ["https://cdn.example.edu/question.png"],
            })

        http.post.assert_not_called()


class VisionFrontendDefaultsTests(unittest.TestCase):
    def test_checkbox_starts_checked_and_saved_false_remains_supported(self):
        class Inputs(HTMLParser):
            def __init__(self):
                super().__init__()
                self.inputs = {}

            def handle_starttag(self, tag, attrs):
                value = dict(attrs)
                if tag == "input" and "id" in value:
                    self.inputs[value["id"]] = value

        html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
        parser = Inputs()
        parser.feed(html)
        self.assertIn("checked", parser.inputs["llmVision"])
        self.assertIn('$("llmVision").checked = settings.vision_enabled !== false', html)
        self.assertIn('vision_enabled: $("llmVision").checked', html)
        self.assertIn('if ($("llmModel").value !== previous) $("llmVision").checked = true', html)
        self.assertNotIn('$("llmVision").checked = false', html)


if __name__ == "__main__":
    unittest.main()
