import asyncio
import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import aiohttp
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

import server
import ykt_core as core


def config(*, dry_run=True):
    return {
        "server": "yuketang",
        "llm": {
            "base_url": "https://example.invalid/v1",
            "api_key": "test",
            "model": "test",
            "temperature": 0.1,
            "vision_enabled": False,
        },
        "lesson": {"poll_interval": 0.01},
        "bot": {
            "dry_run": dry_run,
            "wait_manual_checkin": True,
            "auto_start_watching": True,
            "answer_delay_seconds": 0,
            "safety_seconds": 8,
            "request_margin_seconds": 0.1,
        },
    }


class FakeHub:
    def __init__(self):
        self.state = {
            "phase": "idle",
            "detail": "",
            "dry_run": True,
            "answered": 0,
            "problems": 0,
        }
        self.events = []
        self.logs = []

    async def push(self, kind, **data):
        self.events.append((kind, data))

    async def sync_state(self):
        return None

    def log(self, text, level="info"):
        self.logs.append((level, text))

    def clear_problem(self):
        return None


class FakePanel:
    def __init__(self):
        self.events = []
        self.logs = []
        self.syncs = 0

    async def push_account(self, account_id, kind, **data):
        self.events.append((account_id, kind, data))

    def log_account(self, account_id, account_name, text, level="info"):
        self.logs.append((account_id, account_name, level, text))

    async def sync_state(self):
        self.syncs += 1


class FakeRequest:
    def __init__(self, body=None, *, content_type="application/json", headers=None,
                 scheme="http", host="127.0.0.1:8765"):
        self.body = {} if body is None else body
        self.app = {}
        self.content_type = content_type
        self.headers = headers or {}
        self.scheme = scheme
        self.host = host

    async def json(self):
        return self.body


class FakeLoginWatcher:
    def __init__(self):
        self.running = False
        self.starts = 0
        self.stops = 0

    def start(self):
        self.running = True
        self.starts += 1
        return True

    async def stop(self):
        self.running = False
        self.stops += 1


class FakeClient:
    server_key = "yuketang"
    base = "https://www.yuketang.cn"
    user_id = "u1"
    user_name = "Tester"
    bearer = ""
    lesson_token = ""

    def __init__(self):
        self.checkin_calls = []
        self.submissions = []

    async def checkin(self, lesson_id, join_if_not_in=False):
        self.checkin_calls.append((str(lesson_id), join_if_not_in))
        if join_if_not_in is False:
            return {"ok": True, "bearer": "bearer", "lesson_token": "lesson"}
        raise AssertionError("automatic check-in must never be used")

    async def submit_answer(self, problem_id, dt, problem_type, result, **kwargs):
        self.submissions.append((problem_id, dt, problem_type, result, kwargs, time.time()))
        return {"code": 0}

    async def fetch_presentation(self, presentation_id):
        return None


class ImmediateSolver:
    async def solve(self, problem):
        return {"result": ["A"], "display": '["A"]'}


class BlockingSolver:
    async def solve(self, problem):
        await asyncio.Event().wait()


class CaptureClient(core.YuketangClient):
    def __init__(self):
        super().__init__({"server": "yuketang"}, object())
        self.calls = []

    async def _req(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return 200, {}, {"code": 0}


class CoreTests(unittest.IsolatedAsyncioTestCase):
    def test_chat_completions_url_accepts_base_or_full_endpoint(self):
        self.assertEqual(
            core.chat_completions_url("https://api.example.com/v1/"),
            "https://api.example.com/v1/chat/completions",
        )
        self.assertEqual(
            core.chat_completions_url("https://api.example.com/v1/chat/completions"),
            "https://api.example.com/v1/chat/completions",
        )
        self.assertEqual(
            core.chat_completions_url("http://127.0.0.1:11434/v1"),
            "http://127.0.0.1:11434/v1/chat/completions",
        )
        self.assertEqual(
            core.models_url("https://api.example.com/v1/chat/completions"),
            "https://api.example.com/v1/models",
        )
        self.assertEqual(
            core.api_root_url("https://api.example.com/v1/"),
            core.api_root_url("https://api.example.com/v1/chat/completions"),
        )
        for value in (
            "http://api.example.com/v1",
            "https://user:password@api.example.com/v1",
            "https://api.example.com/v1?token=secret",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                core.chat_completions_url(value)

    async def test_llm_solver_rejects_placeholder_key_before_request(self):
        http = mock.MagicMock()
        cfg = config()
        cfg["llm"]["api_key"] = "YOUR_API_KEY"
        solver = core.LLMSolver(cfg, http)

        with self.assertRaisesRegex(RuntimeError, "API Key 未配置"):
            await solver.solve({"type": 1, "title": "Question", "options": [("A", "One")]})

        http.post.assert_not_called()

    async def test_llm_solver_lists_and_deduplicates_models_without_redirects(self):
        response = mock.MagicMock()
        response.__aenter__ = mock.AsyncMock(return_value=response)
        response.__aexit__ = mock.AsyncMock(return_value=None)
        response.status = 200
        response.json = mock.AsyncMock(return_value={"data": [
            {"id": "model-b"}, {"id": "model-a"}, {"id": "model-a"}, {},
        ]})
        http = mock.MagicMock()
        http.get.return_value = response
        cfg = config()
        cfg["llm"]["base_url"] = "https://api.example.com/v1"
        cfg["llm"]["api_key"] = "secret"
        solver = core.LLMSolver(cfg, http)

        models = await solver.list_models()

        self.assertEqual(models, ["model-a", "model-b"])
        self.assertEqual(http.get.call_args.args[0], "https://api.example.com/v1/models")
        self.assertEqual(http.get.call_args.kwargs["headers"]["authorization"], "Bearer secret")
        self.assertFalse(http.get.call_args.kwargs["allow_redirects"])

    def test_parse_llm_json_accepts_fences_prose_and_content_blocks(self):
        expected = {"answers": ["A"]}
        values = (
            expected,
            '```JSON\n{"answers":["A"]}\n```',
            'Answer follows: {"answers":["A"]} done.',
            [{"type": "output_text", "text": {"value": '{"answers":["A"]}'}}],
            [{"type": "json", "json": expected}],
        )
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(core.parse_llm_json(value), expected)
        for value in (None, "", "not json", []):
            with self.subTest(invalid=value), self.assertRaises(ValueError):
                core.parse_llm_json(value)

    async def test_llm_solver_falls_back_to_reasoning_content(self):
        response = mock.MagicMock()
        response.__aenter__ = mock.AsyncMock(return_value=response)
        response.__aexit__ = mock.AsyncMock(return_value=None)
        response.status = 200
        response.json = mock.AsyncMock(return_value={"choices": [{
            "finish_reason": "stop",
            "message": {"content": None, "reasoning_content": '{"answers":["A"]}'},
        }]})
        http = mock.MagicMock()
        http.post.return_value = response
        solver = core.LLMSolver(config(), http)

        result = await solver.solve({
            "type": 1, "title": "Question", "options": [("A", "One")],
        })

        self.assertEqual(result["result"], ["A"])

    async def test_llm_solver_never_sends_temperature(self):
        response = mock.MagicMock()
        response.__aenter__ = mock.AsyncMock(return_value=response)
        response.__aexit__ = mock.AsyncMock(return_value=None)
        response.status = 200
        response.json = mock.AsyncMock(return_value={"choices": [{
            "finish_reason": "stop",
            "message": {"content": '{"answers":["A"]}'},
        }]})
        http = mock.MagicMock()
        http.post.return_value = response
        cfg = config()
        self.assertEqual(cfg["llm"]["temperature"], 0.1)
        solver = core.LLMSolver(cfg, http)

        await solver.solve({
            "type": 1, "title": "Question", "options": [("A", "One")],
        })

        payload = http.post.call_args.kwargs["json"]
        self.assertNotIn("temperature", payload)

    def test_llm_result_rejects_invalid_or_extra_choice_answers(self):
        options = [("A", "One"), ("B", "Two")]
        invalid_cases = (
            (1, {"answers": ["A", "B"]}),
            (1, {"answers": ["Z"]}),
            (2, {"answers": ["A", "Z"]}),
            (2, {"answers": ["A", "A"]}),
            (2, {"answers": ["A", ""]}),
            (2, {"answers": "A"}),
        )
        for problem_type, parsed in invalid_cases:
            with self.subTest(problem_type=problem_type, parsed=parsed):
                with self.assertRaises(ValueError):
                    core._validate_llm_result({
                        "type": problem_type,
                        "title": "Question",
                        "options": options,
                    }, parsed)

        valid = core._validate_llm_result({
            "type": 2, "title": "Question", "options": options,
        }, {"answers": ["a", "B"]})
        self.assertEqual(valid["result"], ["A", "B"])

    def test_llm_result_validates_fill_blank_count_and_nonempty_values(self):
        problem = {
            "type": 4,
            "title": "[填空1] + 【填空 2】",
            "options": [],
        }
        result = core._validate_llm_result(problem, {"answers": ["one", "two"]})
        self.assertEqual(result["result"], ["one", "two"])

        for answers in (["one"], ["one", "two", "three"], ["one", " "]):
            with self.subTest(answers=answers), self.assertRaises(ValueError):
                core._validate_llm_result(problem, {"answers": answers})

        inferred_unknown = core._validate_llm_result({
            "type": 4, "title": "请填写答案", "options": [],
        }, {"answers": ["nonempty"]})
        self.assertEqual(inferred_unknown["result"], ["nonempty"])

    def test_llm_result_preserves_subjective_submission_structure(self):
        result = core._validate_llm_result({
            "type": 5, "title": "Explain", "options": [],
        }, {"content": "  concise answer  "})

        self.assertEqual(result, {
            "result": {
                "content": "concise answer",
                "pics": [{"pic": "", "thumb": ""}],
                "videos": [],
            },
            "display": "concise answer",
        })

    async def test_llm_solver_rejects_unknown_problem_type_before_request(self):
        http = mock.MagicMock()
        solver = core.LLMSolver(config(), http)

        with self.assertRaisesRegex(ValueError, "不支持的题型"):
            await solver.solve({"type": 99, "title": "Unknown", "options": []})

        http.post.assert_not_called()

    async def test_llm_solver_uses_multimodal_content_only_when_images_exist(self):
        response = mock.MagicMock()
        response.__aenter__ = mock.AsyncMock(return_value=response)
        response.__aexit__ = mock.AsyncMock(return_value=None)
        response.status = 200
        response.json = mock.AsyncMock(return_value={"choices": [{
            "finish_reason": "stop",
            "message": {"content": '{"answers":["A"]}'},
        }]})
        http = mock.MagicMock()
        http.post.return_value = response
        cfg = config()
        cfg["llm"]["vision_enabled"] = True
        solver = core.LLMSolver(cfg, http)
        text_problem = {
            "type": 1, "title": "Question", "options": [("A", "One")],
            "images": [],
        }

        await solver.solve(text_problem)

        payload = http.post.call_args.kwargs["json"]
        self.assertEqual(payload["messages"][1], {
            "role": "user", "content": core.build_prompt(text_problem),
        })

        image_url = "https://cdn.example.edu/slides/question.jpg"
        image_problem = dict(text_problem, images=[image_url])
        await solver.solve(image_problem)

        payload = http.post.call_args.kwargs["json"]
        self.assertEqual(payload["messages"][1], {
            "role": "user",
            "content": [
                {"type": "text", "text": core.build_prompt(image_problem)},
                {"type": "text", "text": "题目图片 1："},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        })
        http.get.assert_not_called()

    async def test_llm_solver_inlines_trusted_yuketang_image_with_bounded_fetch(self):
        image_url = (
            "https://yangtse-pri-ups.yuketang.cn/common_uploads/question.png"
            "?auth_key=signed-value"
        )
        image_bytes = b"\x89PNG\r\n\x1a\ntrusted-image"

        class ImageContent:
            async def read(self, size=-1):
                return image_bytes if size < 0 else image_bytes[:size]

            def iter_chunked(self, size):
                async def chunks():
                    for offset in range(0, len(image_bytes), size):
                        yield image_bytes[offset:offset + size]
                return chunks()

        download = mock.MagicMock()
        download.__aenter__ = mock.AsyncMock(return_value=download)
        download.__aexit__ = mock.AsyncMock(return_value=None)
        download.status = 200
        download.content_type = "image/png"
        download.content_length = len(image_bytes)
        download.headers = {
            "Content-Type": "image/png",
            "content-type": "image/png",
            "Content-Length": str(len(image_bytes)),
            "content-length": str(len(image_bytes)),
        }
        download.content = ImageContent()
        download.read = mock.AsyncMock(return_value=image_bytes)

        completion = mock.MagicMock()
        completion.__aenter__ = mock.AsyncMock(return_value=completion)
        completion.__aexit__ = mock.AsyncMock(return_value=None)
        completion.status = 200
        completion.json = mock.AsyncMock(return_value={"choices": [{
            "finish_reason": "stop",
            "message": {"content": '{"answers":["A"]}'},
        }]})
        http = mock.MagicMock()
        http.get.return_value = download
        http.post.return_value = completion
        cfg = config()
        cfg["llm"]["vision_enabled"] = True
        solver = core.LLMSolver(cfg, http)
        problem = {
            "type": 1,
            "title": "根据下图选择答案",
            "options": [("A", "One"), ("B", "Two")],
            "images": [image_url],
        }

        await solver.solve(problem)

        http.get.assert_called_once()
        self.assertEqual(http.get.call_args.args[0], image_url)
        fetch_options = http.get.call_args.kwargs
        self.assertFalse(fetch_options["allow_redirects"])
        self.assertLessEqual(fetch_options["timeout"].total, 10)
        self.assertNotIn("authorization", {
            str(key).lower(): value for key, value in fetch_options.get("headers", {}).items()
        })
        blocks = http.post.call_args.kwargs["json"]["messages"][1]["content"]
        image_block = next(block for block in blocks if block.get("type") == "image_url")
        self.assertEqual(
            image_block["image_url"]["url"],
            "data:image/png;base64," + base64.b64encode(image_bytes).decode(),
        )

    async def test_llm_solver_does_not_download_untrusted_image_host(self):
        image_url = "https://cdn.example.edu/questions/question.png?token=remote"
        completion = mock.MagicMock()
        completion.__aenter__ = mock.AsyncMock(return_value=completion)
        completion.__aexit__ = mock.AsyncMock(return_value=None)
        completion.status = 200
        completion.json = mock.AsyncMock(return_value={"choices": [{
            "finish_reason": "stop",
            "message": {"content": '{"answers":["A"]}'},
        }]})
        http = mock.MagicMock()
        http.post.return_value = completion
        cfg = config()
        cfg["llm"]["vision_enabled"] = True
        solver = core.LLMSolver(cfg, http)

        await solver.solve({
            "type": 1,
            "title": "Question",
            "options": [("A", "One")],
            "images": [image_url],
        })

        http.get.assert_not_called()
        blocks = http.post.call_args.kwargs["json"]["messages"][1]["content"]
        image_block = next(block for block in blocks if block.get("type") == "image_url")
        self.assertEqual(image_block["image_url"]["url"], image_url)

    async def test_llm_solver_rejects_trusted_image_when_download_fails(self):
        image_url = "https://assets.yuketang.cn/questions/question.jpg?auth_key=signed"
        download = mock.MagicMock()
        download.__aenter__ = mock.AsyncMock(
            side_effect=aiohttp.ClientConnectionError("image unavailable")
        )
        download.__aexit__ = mock.AsyncMock(return_value=None)
        completion = mock.MagicMock()
        completion.__aenter__ = mock.AsyncMock(return_value=completion)
        completion.__aexit__ = mock.AsyncMock(return_value=None)
        completion.status = 200
        completion.json = mock.AsyncMock(return_value={"choices": [{
            "finish_reason": "stop",
            "message": {"content": '{"answers":["A"]}'},
        }]})
        http = mock.MagicMock()
        http.get.return_value = download
        http.post.return_value = completion
        cfg = config()
        cfg["llm"]["vision_enabled"] = True
        solver = core.LLMSolver(cfg, http)

        with self.assertRaisesRegex(RuntimeError, "下载可信题目图片失败"):
            await solver.solve({
                "type": 1,
                "title": "根据图片选择",
                "options": [("A", "One")],
                "images": [image_url],
            })

        http.get.assert_called_once()
        http.post.assert_not_called()

    async def test_llm_solver_rejects_redirected_trusted_image(self):
        image_url = "https://assets.xuetangx.com/questions/question.jpg"
        download = mock.MagicMock()
        download.__aenter__ = mock.AsyncMock(return_value=download)
        download.__aexit__ = mock.AsyncMock(return_value=None)
        download.status = 302
        download.headers = {"Location": "https://storage.example/question.jpg"}
        http = mock.MagicMock()
        http.get.return_value = download
        cfg = config()
        cfg["llm"]["vision_enabled"] = True
        solver = core.LLMSolver(cfg, http)

        with self.assertRaisesRegex(RuntimeError, "HTTP 302"):
            await solver.solve({
                "type": 1,
                "title": "根据图片选择",
                "options": [("A", "One")],
                "images": [image_url],
            })

        self.assertFalse(http.get.call_args.kwargs["allow_redirects"])
        http.post.assert_not_called()

    async def test_llm_solver_rejects_trusted_image_with_mismatched_format(self):
        image_url = "https://assets.yuketang.cn/questions/question.jpg"
        image_bytes = b"\x89PNG\r\n\x1a\nactual-png"

        class ImageContent:
            def iter_chunked(self, size):
                async def chunks():
                    yield image_bytes
                return chunks()

        download = mock.MagicMock()
        download.__aenter__ = mock.AsyncMock(return_value=download)
        download.__aexit__ = mock.AsyncMock(return_value=None)
        download.status = 200
        download.headers = {
            "Content-Type": "image/jpeg",
            "Content-Length": str(len(image_bytes)),
        }
        download.content = ImageContent()
        http = mock.MagicMock()
        http.get.return_value = download
        cfg = config()
        cfg["llm"]["vision_enabled"] = True
        solver = core.LLMSolver(cfg, http)

        with self.assertRaisesRegex(RuntimeError, "声明格式与实际内容不一致"):
            await solver.solve({
                "type": 1,
                "title": "根据图片选择",
                "options": [("A", "One")],
                "images": [image_url],
            })

        http.post.assert_not_called()

    async def test_llm_solver_rejects_declared_oversized_trusted_image_without_reading(self):
        image_url = "https://assets.yuketang.cn/questions/huge.jpg"
        download = mock.MagicMock()
        download.__aenter__ = mock.AsyncMock(return_value=download)
        download.__aexit__ = mock.AsyncMock(return_value=None)
        download.status = 200
        download.content_type = "image/jpeg"
        download.content_length = 100_000_000
        download.headers = {
            "Content-Type": "image/jpeg",
            "Content-Length": "100000000",
        }
        download.read = mock.AsyncMock(side_effect=AssertionError("oversized body was read"))
        download.content.read = mock.AsyncMock(side_effect=AssertionError("oversized body was read"))

        completion = mock.MagicMock()
        completion.__aenter__ = mock.AsyncMock(return_value=completion)
        completion.__aexit__ = mock.AsyncMock(return_value=None)
        completion.status = 200
        completion.json = mock.AsyncMock(return_value={"choices": [{
            "finish_reason": "stop",
            "message": {"content": '{"answers":["A"]}'},
        }]})
        http = mock.MagicMock()
        http.get.return_value = download
        http.post.return_value = completion
        cfg = config()
        cfg["llm"]["vision_enabled"] = True
        solver = core.LLMSolver(cfg, http)

        with self.assertRaisesRegex(RuntimeError, "超过内联大小限制"):
            await solver.solve({
                "type": 1,
                "title": "根据图片选择",
                "options": [("A", "One")],
                "images": [image_url],
            })

        download.read.assert_not_awaited()
        download.content.read.assert_not_awaited()
        http.post.assert_not_called()

    async def test_llm_solver_limits_total_trusted_image_downloads_to_twelve_mib(self):
        image_size = 4 * 1024 * 1024
        total_read = [0] * 8
        image_urls = [
            f"https://assets.yuketang.cn/questions/{index}.png"
            for index in range(8)
        ]

        class CountingContent:
            def __init__(self, index):
                self.index = index

            def iter_chunked(self, chunk_size):
                async def chunks():
                    remaining = image_size
                    first = True
                    while remaining:
                        size = min(chunk_size, remaining)
                        if first:
                            prefix = b"\x89PNG\r\n\x1a\n"
                            chunk = prefix + bytes(size - len(prefix))
                            first = False
                        else:
                            chunk = bytes(size)
                        total_read[self.index] += len(chunk)
                        remaining -= len(chunk)
                        yield chunk
                return chunks()

        downloads = {}
        for index, image_url in enumerate(image_urls):
            response = mock.MagicMock()
            response.__aenter__ = mock.AsyncMock(return_value=response)
            response.__aexit__ = mock.AsyncMock(return_value=None)
            response.status = 200
            response.headers = {
                "Content-Type": "image/png",
                "Content-Length": str(image_size),
            }
            response.content = CountingContent(index)
            downloads[image_url] = response

        completion = mock.MagicMock()
        completion.__aenter__ = mock.AsyncMock(return_value=completion)
        completion.__aexit__ = mock.AsyncMock(return_value=None)
        completion.status = 200
        completion.json = mock.AsyncMock(return_value={"choices": [{
            "finish_reason": "stop",
            "message": {"content": '{"answers":["A"]}'},
        }]})
        http = mock.MagicMock()
        http.get.side_effect = lambda url, **kwargs: downloads[url]
        http.post.return_value = completion
        cfg = config()
        cfg["llm"]["vision_enabled"] = True
        solver = core.LLMSolver(cfg, http)

        with self.assertRaisesRegex(RuntimeError, "超过内联总大小限制"):
            await solver.solve({
                "type": 1,
                "title": "根据图片选择",
                "options": [("A", "One")],
                "images": image_urls,
            })

        self.assertLessEqual(sum(total_read), 12 * 1024 * 1024)
        self.assertEqual(total_read[:3], [image_size] * 3)
        self.assertEqual(total_read[3:], [0] * 5)
        self.assertEqual(http.get.call_count, 3)
        http.post.assert_not_called()

    async def test_llm_solver_limits_original_data_urls_and_removes_their_labels(self):
        image_size = 2 * 1024 * 1024
        data_urls = []
        image_items = []
        for index in range(8):
            raw = b"\x89PNG\r\n\x1a\n" + bytes([index]) + bytes(image_size - 9)
            data_url = "data:image/png;base64," + base64.b64encode(raw).decode()
            data_urls.append(data_url)
            image_items.append({
                "url": data_url,
                "label": f"原始图片 {index + 1}",
                "kind": "question_image",
                "priority": 10,
            })

        completion = mock.MagicMock()
        completion.__aenter__ = mock.AsyncMock(return_value=completion)
        completion.__aexit__ = mock.AsyncMock(return_value=None)
        completion.status = 200
        completion.json = mock.AsyncMock(return_value={"choices": [{
            "finish_reason": "stop",
            "message": {"content": '{"answers":["A"]}'},
        }]})
        http = mock.MagicMock()
        http.post.return_value = completion
        cfg = config()
        cfg["llm"]["vision_enabled"] = True
        solver = core.LLMSolver(cfg, http)

        await solver.solve({
            "type": 1,
            "title": "根据图片选择",
            "options": [("A", "One")],
            "images": data_urls,
            "image_items": image_items,
        })

        http.get.assert_not_called()
        blocks = http.post.call_args.kwargs["json"]["messages"][1]["content"]
        sent_urls = [
            block["image_url"]["url"]
            for block in blocks if block.get("type") == "image_url"
        ]
        self.assertEqual(sent_urls, data_urls[:6])
        decoded_total = sum(
            len(base64.b64decode(url.split(",", 1)[1])) for url in sent_urls
        )
        self.assertLessEqual(decoded_total, 12 * 1024 * 1024)
        label_text = " ".join(
            block.get("text", "") for block in blocks if block.get("type") == "text"
        )
        self.assertIn("原始图片 6", label_text)
        self.assertNotIn("原始图片 7", label_text)
        self.assertNotIn("原始图片 8", label_text)

    async def test_llm_solver_rejects_images_when_vision_is_disabled_before_request(self):
        http = mock.MagicMock()
        solver = core.LLMSolver(config(), http)

        with self.assertRaisesRegex(RuntimeError, "视觉|图片"):
            await solver.solve({
                "type": 1,
                "title": "Choose from the diagram",
                "options": [("A", "One"), ("B", "Two")],
                "images": ["https://cdn.example.edu/slides/question.jpg"],
            })

        http.post.assert_not_called()

    async def test_llm_solver_reports_non_json_http_response_clearly(self):
        response = mock.MagicMock()
        response.__aenter__ = mock.AsyncMock(return_value=response)
        response.__aexit__ = mock.AsyncMock(return_value=None)
        response.status = 200
        response.json = mock.AsyncMock(side_effect=json.JSONDecodeError("bad", "<html>", 0))
        http = mock.MagicMock()
        http.post.return_value = response
        solver = core.LLMSolver(config(), http)

        with self.assertRaisesRegex(RuntimeError, "模型服务返回了非 JSON 响应"):
            await solver.solve({
                "type": 1, "title": "Question", "options": [("A", "One")],
            })

    async def test_llm_http_error_includes_sanitized_flattened_details(self):
        api_key = "test-key-SUPER-SECRET-123"
        query_secret = "QUERY-SECRET-456"
        encoded_secret = base64.b64encode(b"DATA-URI-SECRET-PAYLOAD").decode()
        tail_marker = "TAIL-MUST-BE-TRUNCATED"
        signed_url = (
            "https://assets.yuketang.cn/question.png"
            f"?auth_key={query_secret}&token=another-secret"
        )
        response = mock.MagicMock()
        response.__aenter__ = mock.AsyncMock(return_value=response)
        response.__aexit__ = mock.AsyncMock(return_value=None)
        response.status = 400
        response.json = mock.AsyncMock(return_value={"error": {
            "message": (
                "Image\nfetch\tfailed for " + signed_url
                + " data:image/png;base64," + encoded_secret
                + " api_key=" + api_key
                + " " + ("x" * 5000) + tail_marker
            ),
            "type": "invalid_request_error",
            "param": "messages[1].content[2].image_url",
            "code": "invalid_image_url",
        }})
        http = mock.MagicMock()
        http.post.return_value = response
        cfg = config()
        cfg["llm"]["api_key"] = api_key
        solver = core.LLMSolver(cfg, http)

        with self.assertRaises(RuntimeError) as raised:
            await solver.solve({
                "type": 1,
                "title": "Question",
                "options": [("A", "One")],
            })

        message = str(raised.exception)
        self.assertIn("LLM HTTP 400", message)
        self.assertIn("Image fetch failed", message)
        self.assertIn("invalid_request_error", message)
        self.assertIn("messages[1].content[2].image_url", message)
        self.assertIn("invalid_image_url", message)
        self.assertNotIn("\n", message)
        self.assertNotIn("\t", message)
        self.assertNotIn(api_key, message)
        self.assertNotIn(query_secret, message)
        self.assertNotIn("another-secret", message)
        self.assertNotIn(encoded_secret, message)
        self.assertNotIn(tail_marker, message)
        self.assertLess(len(message), 2000)

    def test_qr_image_normalization(self):
        url = "https://mp.weixin.qq.com/cgi-bin/showqrcode?ticket=abc"
        self.assertEqual(core.normalize_qr_image(url), url)
        data_uri = "data:image/png;base64,AAAA"
        self.assertEqual(core.normalize_qr_image(data_uri), data_uri)
        png = base64.b64encode(b"\x89PNG\r\n\x1a\ncontent").decode()
        self.assertEqual(core.normalize_qr_image(png), "data:image/png;base64," + png)
        self.assertEqual(core.normalize_qr_image("not an image"), "")

    def test_extracts_direct_and_nested_problems(self):
        payload = {"data": {"problems": [
            {"prob": 7, "type": 1, "title": "<b>2 + 2</b>", "options": ["4", "5"]},
            {"problem": {"problemId": "8", "problemType": 4, "content": "[填空1]"}},
        ]}}
        problems = core.extract_problems(payload)
        self.assertEqual(set(problems), {"7", "8"})
        self.assertEqual(problems["7"]["title"], "2 + 2")
        self.assertEqual(problems["7"]["options"][0], ("A", "4"))

    def test_visual_body_inherits_parent_slide_cover(self):
        cover = "https://cdn.example.edu/slides/question.jpg?signature=abc"
        payload = {"data": {"slides": [{
            "cover": cover,
            "problem": {
                "problemId": "body-image",
                "problemType": 1,
                "body": "<p>根据下图，温度与压强的关系是&nbsp;什么？</p>",
                "options": [
                    {"key": "A", "value": "正相关"},
                    {"key": "B", "value": "无关"},
                ],
            },
        }]}}

        problem = core.extract_problems(payload)["body-image"]

        self.assertEqual(problem["title"], "根据下图，温度与压强的关系是 什么？")
        self.assertEqual(problem["images"], [cover])

    def test_significant_picture_shape_makes_implicit_visual_problem_keep_cover(self):
        visual_cover = "https://cdn.example.edu/slides/instrument.jpg"
        text_cover = "https://cdn.example.edu/slides/text-only.jpg"
        problem = {
            "type": 1,
            "body": "该仪器的读数为多少？",
            "options": [
                {"key": "A", "value": "10"},
                {"key": "B", "value": "20"},
            ],
        }
        payload = {"slides": [
            {
                "cover": visual_cover,
                "shapes": [
                    {"PPTShapeType": 17, "Width": 500, "Height": 80},
                    {"PPTShapeType": 13, "Width": 400, "Height": 300},
                ],
                "problem": {"prob": "implicit-visual", **problem},
            },
            {
                "cover": text_cover,
                "shapes": [
                    {"PPTShapeType": 17, "Width": 800, "Height": 500},
                    {"PPTShapeType": 17, "Width": 600, "Height": 100},
                ],
                "problem": {"prob": "text-shapes-only", **problem},
            },
        ]}

        problems = core.extract_problems(payload)

        self.assertEqual(problems["implicit-visual"]["images"], [visual_cover])
        self.assertEqual(problems["text-shapes-only"]["images"], [])
        self.assertEqual(problems["text-shapes-only"]["image_items"], [])

    def test_significant_ppt_drawing_shapes_keep_cover_but_small_decorations_do_not(self):
        problem = {
            "prob": "drawing-question",
            "type": 1,
            "body": "请选择正确答案。",
            "options": [
                {"key": "A", "value": "甲"},
                {"key": "B", "value": "乙"},
            ],
        }
        shape_cases = {
            "group": [6],
            "autoshape": [1],
            "freeform": [5],
            "line": [9],
            "combined": [6, 1, 5, 9],
        }

        for name, shape_types in shape_cases.items():
            with self.subTest(significant=name):
                cover = f"https://cdn.example.edu/slides/{name}.jpg"
                shapes = [
                    {"PPTShapeType": shape_type, "Width": 400, "Height": 300}
                    for shape_type in shape_types
                ]
                extracted = core.extract_problems({
                    "slides": [{"cover": cover, "shapes": shapes, "problem": problem}],
                })["drawing-question"]
                self.assertEqual(extracted["images"], [cover])

        decorative_cover = "https://cdn.example.edu/slides/decorations.jpg"
        decorative_shapes = [
            {"PPTShapeType": shape_type, "Width": 20, "Height": 20}
            for shape_type in (6, 1, 5, 9)
        ]
        extracted = core.extract_problems({
            "slides": [{
                "cover": decorative_cover,
                "shapes": decorative_shapes,
                "problem": problem,
            }],
        })["drawing-question"]
        self.assertEqual(extracted["images"], [])
        self.assertEqual(extracted["image_items"], [])

    async def test_complete_text_problem_ignores_slide_cover_and_sends_text_content(self):
        cover = "https://cdn.example.edu/slides/decorative-cover.jpg"
        payload = {"slides": [{
            "cover": cover,
            "problem": {
                "prob": "plain-text",
                "type": 1,
                "body": "二加二等于多少？",
                "options": [
                    {"key": "A", "value": "四"},
                    {"key": "B", "value": "五"},
                ],
            },
        }]}
        problem = core.extract_problems(payload)["plain-text"]
        self.assertEqual(problem["images"], [])
        self.assertEqual(problem["image_items"], [])

        response = mock.MagicMock()
        response.__aenter__ = mock.AsyncMock(return_value=response)
        response.__aexit__ = mock.AsyncMock(return_value=None)
        response.status = 200
        response.json = mock.AsyncMock(return_value={"choices": [{
            "finish_reason": "stop",
            "message": {"content": '{"answers":["A"]}'},
        }]})
        http = mock.MagicMock()
        http.post.return_value = response
        solver = core.LLMSolver(config(), http)

        result = await solver.solve(problem)

        self.assertEqual(result["result"], ["A"])
        user_content = http.post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertIsInstance(user_content, str)
        self.assertEqual(user_content, core.build_prompt(problem))

    def test_incomplete_problem_text_keeps_parent_slide_cover(self):
        cover = "https://cdn.example.edu/slides/fallback-cover.jpg"
        cases = (
            {
                "prob": "missing-title",
                "type": 1,
                "body": "",
                "options": [
                    {"key": "A", "value": "One"},
                    {"key": "B", "value": "Two"},
                ],
            },
            {
                "prob": "missing-option-text",
                "type": 1,
                "body": "请选择正确答案",
                "options": [
                    {"key": "A", "value": "One"},
                    {"key": "B", "value": ""},
                ],
            },
        )

        for raw_problem in cases:
            with self.subTest(problem_id=raw_problem["prob"]):
                problem = core.extract_problems({
                    "slides": [{"cover": cover, "problem": raw_problem}],
                })[raw_problem["prob"]]
                self.assertEqual(problem["images"], [cover])
                self.assertEqual(problem["image_items"][0]["url"], cover)

    def test_problem_images_reject_unsafe_urls(self):
        safe = "https://cdn.example.edu/questions/safe.png?signature=abc"
        protocol_relative = "https://cdn.example.edu/questions/relative.webp"
        payload = {"slides": [{
            "cover": "http://cdn.example.edu/questions/insecure-cover.jpg",
            "problem": {
                "prob": "filtered-images",
                "type": 1,
                "body": "Question",
                "images": [
                    "javascript:alert(1)",
                    "http://cdn.example.edu/questions/insecure.png",
                    "https://user:password@cdn.example.edu/private.png",
                    "data:image/png;base64,not-valid-base64!",
                    safe,
                    "//cdn.example.edu/questions/relative.webp",
                ],
            },
        }]}

        problem = core.extract_problems(payload)["filtered-images"]

        self.assertEqual(problem["images"], [safe, protocol_relative])

    def test_problem_specific_images_do_not_leak_to_siblings_on_same_slide(self):
        cover = "https://cdn.example.edu/slides/shared-cover.jpg"
        first_only = "https://cdn.example.edu/questions/first-only.png"
        payload = {"slides": [{
            "cover": cover,
            "problems": [
                {"prob": "first", "type": 1, "body": "First", "images": [first_only]},
                {"prob": "second", "type": 1, "body": "Second"},
            ],
        }]}

        problems = core.extract_problems(payload)

        self.assertEqual(problems["first"]["images"], [cover, first_only])
        self.assertEqual(problems["second"]["images"], [cover])

    def test_pure_image_options_keep_all_labels_in_multimodal_content(self):
        cover = "https://cdn.example.edu/slides/question-cover.jpg"
        option_urls = {
            key: f"https://cdn.example.edu/options/{key.lower()}.png"
            for key in ("A", "B", "C", "D")
        }
        payload = {"slides": [{
            "cover": cover,
            "problem": {
                "prob": "four-image-options",
                "type": 1,
                "body": "请选择正确的示意图",
                "options": [
                    {"key": key, "value": f'<img src="{url}">' }
                    for key, url in option_urls.items()
                ],
            },
        }]}

        problem = core.extract_problems(payload)["four-image-options"]
        option_items = [
            item for item in problem["image_items"]
            if item["url"] in option_urls.values()
        ]
        self.assertEqual({item["url"] for item in option_items}, set(option_urls.values()))
        labels = {item["url"]: item["label"] for item in option_items}
        for key, url in option_urls.items():
            with self.subTest(option=key):
                self.assertIn(key, labels[url])

        content = core.build_user_content(problem)
        image_indices = {
            block["image_url"]["url"]: index
            for index, block in enumerate(content)
            if block.get("type") == "image_url"
        }
        for key, url in option_urls.items():
            with self.subTest(content_option=key):
                self.assertIn(url, image_indices)
                label_block = content[image_indices[url] - 1]
                self.assertEqual(label_block["type"], "text")
                self.assertIn(labels[url], label_block["text"])

    async def test_retry_uses_documented_wrapper(self):
        client = CaptureClient()
        await client.submit_answer("p1", 123, 2, ["A", "C"], retry=True, timeout=1)
        method, path, kwargs = client.calls[-1]
        self.assertEqual((method, path), ("POST", "/api/v3/lesson/problem/retry"))
        self.assertEqual(kwargs["json"], {"problems": [{
            "problemId": "p1", "dt": 123, "problemType": 2,
            "result": ["A", "C"], "retry_times": None,
        }]})

    async def test_restored_cookie_is_scoped_to_yuketang(self):
        async with aiohttp.ClientSession() as session:
            client = core.YuketangClient({"server": "yuketang"}, session)
            with mock.patch.object(client, "check_session", mock.AsyncMock(return_value=True)):
                self.assertTrue(await client.restore_cookie("secret"))
            rain = session.cookie_jar.filter_cookies(URL("https://www.yuketang.cn"))
            llm = session.cookie_jar.filter_cookies(URL("https://open.bigmodel.cn"))
            self.assertEqual(rain["x_access_token"].value, "secret")
            self.assertNotIn("x_access_token", llm)

    async def test_restore_session_accepts_qr_session_cookies_without_access_token(self):
        async with aiohttp.ClientSession() as session:
            client = core.YuketangClient({"server": "yuketang"}, session)
            check_session = mock.AsyncMock(return_value=True)

            with mock.patch.object(client, "check_session", check_session):
                restored = await client.restore_session({
                    "cookies": {"sessionid": "session-secret", "sid": "sid-secret"},
                })

            self.assertTrue(restored)
            check_session.assert_awaited_once_with()
            rain = session.cookie_jar.filter_cookies(URL(client.base))
            self.assertEqual(rain["sessionid"].value, "session-secret")
            self.assertEqual(rain["sid"].value, "sid-secret")

    async def test_get_on_lesson_prefers_upcoming_exam_endpoint(self):
        client = core.YuketangClient({"server": "yuketang"}, object())
        room = {"lessonId": "lesson-new", "courseName": "Current Course"}
        request = mock.AsyncMock(return_value=(200, {}, {
            "code": 0, "data": {"onLessonClassrooms": [room]},
        }))

        with mock.patch.object(client, "_req", request):
            rooms = await client.get_on_lesson()

        self.assertEqual(rooms, [room])
        request.assert_awaited_once_with(
            "GET", "/api/v3/classroom/on-lesson-upcoming-exam"
        )

    async def test_get_on_lesson_returns_empty_latest_response(self):
        client = core.YuketangClient({"server": "yuketang"}, object())
        request = mock.AsyncMock(return_value=(200, {}, {
            "code": 0,
            "data": {"onLessonClassrooms": [], "upcomingExam": []},
        }))

        with mock.patch.object(client, "_req", request):
            rooms = await client.get_on_lesson()

        self.assertEqual(rooms, [])
        request.assert_awaited_once_with(
            "GET", "/api/v3/classroom/on-lesson-upcoming-exam"
        )
        self.assertEqual(client.lesson_discovery["latest"]["count"], 0)

    async def test_get_on_lesson_does_not_hide_latest_endpoint_failure(self):
        client = core.YuketangClient({"server": "yuketang"}, object())
        request = mock.AsyncMock(return_value=(503, {}, {
            "code": 503, "message": "temporarily unavailable",
        }))

        with mock.patch.object(client, "_req", request):
            with self.assertRaisesRegex(RuntimeError, r"HTTP 503, code=503"):
                await client.get_on_lesson()

        request.assert_awaited_once_with(
            "GET", "/api/v3/classroom/on-lesson-upcoming-exam"
        )

    async def test_get_on_lesson_rejects_missing_classroom_list(self):
        client = core.YuketangClient({"server": "yuketang"}, object())
        request = mock.AsyncMock(return_value=(200, {}, {
            "code": 0, "data": {"upcomingExam": []},
        }))

        with mock.patch.object(client, "_req", request):
            with self.assertRaisesRegex(RuntimeError, r"HTTP 200, code=0"):
                await client.get_on_lesson()

    async def test_get_on_lesson_deduplicates_lesson_ids(self):
        client = core.YuketangClient({"server": "yuketang"}, object())
        request = mock.AsyncMock(return_value=(200, {}, {
            "code": 0,
            "data": {"onLessonClassrooms": [
                {"lessonId": "lesson-1", "courseName": "First"},
                {"lesson_id": "lesson-1", "courseName": "Duplicate"},
                {"lessonId": "lesson-2", "courseName": "Second"},
            ]},
        }))

        with mock.patch.object(client, "_req", request):
            rooms = await client.get_on_lesson()

        self.assertEqual(
            [(room.get("lessonId") or room.get("lesson_id")) for room in rooms],
            ["lesson-1", "lesson-2"],
        )
        self.assertEqual(rooms[0]["courseName"], "First")

    async def test_request_headers_mirror_rainclassroom_session_cookies(self):
        async with aiohttp.ClientSession() as session:
            client = core.YuketangClient({"server": "yuketang"}, session)
            session.cookie_jar.update_cookies(
                {"csrftoken": "csrf-secret", "sessionid": "session-secret"},
                response_url=URL(client.base),
            )
            session.cookie_jar.update_cookies(
                {"csrftoken": "foreign-csrf", "sessionid": "foreign-session"},
                response_url=URL("https://foreign.example"),
            )

            anonymous = client._headers()
            self.assertEqual(anonymous["x-csrftoken"], "csrf-secret")
            self.assertEqual(anonymous["x-uid"], "")
            self.assertEqual(anonymous["sessionid"], "session-secret")

            session.cookie_jar.update_cookies(
                {"csrftoken": "csrf-next", "sessionid": "session-next"},
                response_url=URL(client.base),
            )
            client.user_id = "user-43"
            updated = client._headers()
            self.assertEqual(updated["x-csrftoken"], "csrf-next")
            self.assertEqual(updated["x-uid"], "user-43")
            self.assertEqual(updated["sessionid"], "session-next")

    async def test_qr_start_identifies_wechat_provider(self):
        client = core.YuketangClient({"server": "yuketang"}, object())
        payload = {"code": 0, "data": {
            "token": "fresh-token",
            "qrContent": "http://weixin.qq.com/q/example",
            "qrImage": "https://mp.weixin.qq.com/cgi-bin/showqrcode?ticket=example",
        }}
        with mock.patch.object(client, "_req", mock.AsyncMock(return_value=(200, {}, payload))):
            result = await client.qr_start()

        self.assertEqual(result["scan_app"], "wechat")

    async def test_qr_timeout_and_expired_token_request_refresh(self):
        client = core.YuketangClient({"server": "yuketang"}, object())
        cases = (
            (200, {"code": 50001, "message": "SCAN_QR_CODE_TIMEOUT"}),
            (400, {"code": 50400, "msg": "BAD_REQUEST"}),
        )
        for status, payload in cases:
            with self.subTest(code=payload["code"]):
                with mock.patch.object(
                    client, "_req", mock.AsyncMock(return_value=(status, {}, payload))
                ):
                    result = await client.qr_poll("old-token")
                self.assertEqual(result["status"], "refresh")
                self.assertEqual(result["code"], payload["code"])

    async def test_checkin_returns_credentials_without_mutating_shared_state(self):
        response = mock.MagicMock()
        response.__aenter__ = mock.AsyncMock(return_value=response)
        response.__aexit__ = mock.AsyncMock(return_value=None)
        response.json = mock.AsyncMock(return_value={
            "code": 0,
            "data": {"lessonToken": "fresh-lesson"},
        })
        response.headers = {"set-auth": "fresh-bearer"}
        http = mock.MagicMock()
        http.post.return_value = response
        client = core.YuketangClient({"server": "yuketang"}, http)
        client.bearer = "current-bearer"
        client.lesson_token = "current-lesson"

        result = await client.checkin("lesson-1")

        self.assertEqual(result, {
            "ok": True,
            "bearer": "fresh-bearer",
            "lesson_token": "fresh-lesson",
        })
        self.assertEqual(client.bearer, "current-bearer")
        self.assertEqual(client.lesson_token, "current-lesson")
        self.assertEqual(http.post.call_args.kwargs["json"], {
            "source": 21,
            "lessonId": "lesson-1",
            "joinIfNotIn": False,
        })

    async def test_adopt_login_copies_only_rainclassroom_cookies(self):
        async with aiohttp.ClientSession() as main_http, aiohttp.ClientSession() as qr_http:
            main = core.YuketangClient(config(), main_http)
            isolated = core.YuketangClient(config(), qr_http)
            qr_http.cookie_jar.update_cookies(
                {
                    "x_access_token": "rain-secret",
                    "sessionid": "session-secret",
                    "sid": "sid-secret",
                    "csrftoken": "csrf-secret",
                },
                response_url=URL(isolated.base),
            )
            qr_http.cookie_jar.update_cookies(
                {"foreign": "must-not-copy"}, response_url=URL("https://llm.example")
            )
            isolated.user_id = "qr-user"
            isolated.user_name = "QR User"

            main.adopt_login(isolated)

            rain = main_http.cookie_jar.filter_cookies(URL(main.base))
            foreign = main_http.cookie_jar.filter_cookies(URL("https://llm.example"))
            self.assertEqual(rain["x_access_token"].value, "rain-secret")
            self.assertEqual(rain["sessionid"].value, "session-secret")
            self.assertEqual(rain["sid"].value, "sid-secret")
            self.assertEqual(rain["csrftoken"].value, "csrf-secret")
            self.assertNotIn("foreign", foreign)
            self.assertEqual((main.user_id, main.user_name), ("qr-user", "QR User"))


class WebSocketOriginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        names = ("cfg", "hub", "client", "watcher", "account_manager")
        self.originals = {name: getattr(server, name) for name in names}
        server.cfg = config()
        server.hub = server.Hub()
        server.client = FakeClient()
        server.watcher = FakeLoginWatcher()
        server.account_manager = None
        app = server.web.Application()
        app.router.add_get("/ws", server.ws_handler)
        self.http_client = TestClient(TestServer(app))
        await self.http_client.start_server()

    async def asyncTearDown(self):
        await self.http_client.close()
        for name, value in self.originals.items():
            setattr(server, name, value)

    async def test_websocket_rejects_cross_origin_handshake(self):
        websocket = None
        try:
            with self.assertRaises(aiohttp.WSServerHandshakeError) as raised:
                websocket = await self.http_client.ws_connect(
                    "/ws", headers={"Origin": "https://attacker.example"},
                )
        finally:
            if websocket is not None:
                await websocket.close()

        self.assertEqual(raised.exception.status, 403)

    async def test_websocket_accepts_same_origin_handshake(self):
        url = self.http_client.make_url("/")
        origin = f"{url.scheme}://{url.host}:{url.port}"

        websocket = await self.http_client.ws_connect("/ws", headers={"Origin": origin})
        try:
            message = await websocket.receive(timeout=1)
            self.assertEqual(message.type, aiohttp.WSMsgType.TEXT)
            self.assertEqual(json.loads(message.data)["kind"], "state")
        finally:
            await websocket.close()


class QRLoginIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        names = ("cfg", "hub", "client", "watcher", "http_session", "llm_session",
                 "qr_generation", "qr_attempt", "SESSION_PATH")
        self.originals = {name: getattr(server, name) for name in names}
        self.tempdir = tempfile.TemporaryDirectory()
        server.cfg = config()
        server.cfg["bot"]["auto_start_watching"] = False
        server.hub = FakeHub()
        self.main_http = aiohttp.ClientSession()
        self.llm_http = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
        server.http_session = self.main_http
        server.llm_session = self.llm_http
        server.client = core.YuketangClient(server.cfg, self.main_http)
        server.watcher = FakeLoginWatcher()
        server.qr_generation = 0
        server.qr_attempt = None
        server.SESSION_PATH = Path(self.tempdir.name) / "session.json"

    async def asyncTearDown(self):
        await server._supersede_qr_attempt()
        await self.main_http.close()
        await self.llm_http.close()
        self.tempdir.cleanup()
        for name, value in self.originals.items():
            setattr(server, name, value)

    async def start_attempt(self, token="qr-token"):
        result = {
            "token": token,
            "qr_image": "https://www.yuketang.cn/qr.jpg",
            "qr_dataurl": "https://www.yuketang.cn/qr.jpg",
        }
        with mock.patch.object(core.YuketangClient, "qr_start", mock.AsyncMock(return_value=result)):
            response = await server.api_login_qr_start(FakeRequest())
        self.assertEqual(response.status, 200)
        return json.loads(response.text), server.qr_attempt

    async def test_success_adopts_isolated_login_into_existing_main_client(self):
        started, attempt = await self.start_attempt()
        main_client = server.client
        self.assertIsNot(attempt.client, main_client)
        self.assertIsNot(attempt.session, self.main_http)

        async def qr_poll(token):
            self.assertEqual(token, started["token"])
            attempt.session.cookie_jar.update_cookies(
                {"x_access_token": "isolated-secret"}, response_url=URL(attempt.client.base)
            )
            attempt.session.cookie_jar.update_cookies(
                {"llm-cookie": "do-not-copy"}, response_url=URL("https://llm.example")
            )
            attempt.client.user_id = "u-qr"
            attempt.client.user_name = "QR User"
            return {"user_id": "u-qr"}

        attempt.client.qr_poll = qr_poll
        response = await server.api_login_qr_poll(FakeRequest({
            "token": started["token"], "attempt_id": started["attempt_id"],
        }))

        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.text)["status"], "ok")
        self.assertIs(server.client, main_client)
        self.assertEqual((main_client.user_id, main_client.user_name), ("u-qr", "QR User"))
        rain = self.main_http.cookie_jar.filter_cookies(URL(main_client.base))
        llm = self.llm_http.cookie_jar.filter_cookies(URL(main_client.base))
        foreign = self.main_http.cookie_jar.filter_cookies(URL("https://llm.example"))
        self.assertEqual(rain["x_access_token"].value, "isolated-secret")
        self.assertNotIn("x_access_token", llm)
        self.assertNotIn("llm-cookie", foreign)
        self.assertIsNone(server.qr_attempt)
        self.assertTrue(attempt.session.closed)

    async def test_poll_requires_attempt_id(self):
        started, _ = await self.start_attempt()
        response = await server.api_error_middleware(
            FakeRequest({"token": started["token"]}), server.api_login_qr_poll
        )
        self.assertEqual(response.status, 400)
        self.assertIn("缺少二维码 attempt_id", json.loads(response.text)["message"])

    async def test_refresh_retires_attempt_without_failing_or_adopting_it(self):
        started, attempt = await self.start_attempt()
        main_client = server.client
        attempt.client.user_id = "expired-user"
        attempt.session.cookie_jar.update_cookies(
            {"x_access_token": "must-not-adopt"}, response_url=URL(attempt.client.base)
        )
        attempt.client.qr_poll = mock.AsyncMock(return_value={
            "status": "refresh", "code": 50001, "message": "SCAN_QR_CODE_TIMEOUT",
        })

        response = await server.api_login_qr_poll(FakeRequest({
            "token": started["token"], "attempt_id": started["attempt_id"],
        }))
        payload = json.loads(response.text)

        self.assertEqual(response.status, 200)
        self.assertEqual((payload["status"], payload["code"]), ("refresh", 50001))
        self.assertIs(server.client, main_client)
        self.assertEqual(server.client.user_id, "")
        self.assertNotIn(
            "x_access_token", self.main_http.cookie_jar.filter_cookies(URL(server.client.base))
        )
        self.assertIsNone(server.qr_attempt)
        self.assertTrue(attempt.session.closed)
        self.assertFalse(any(kind == "login" for kind, _ in server.hub.events))

    async def test_stale_refresh_is_superseded_without_retiring_attempt(self):
        started, attempt = await self.start_attempt()

        async def stale_refresh(_token):
            server.qr_generation += 1
            return {"status": "refresh", "code": 50001}

        attempt.client.qr_poll = stale_refresh
        response = await server.api_login_qr_poll(FakeRequest({
            "token": started["token"], "attempt_id": started["attempt_id"],
        }))

        self.assertEqual(response.status, 409)
        self.assertEqual(json.loads(response.text)["status"], "superseded")
        self.assertIs(server.qr_attempt, attempt)
        self.assertFalse(attempt.session.closed)
        self.assertFalse(any(kind == "login" for kind, _ in server.hub.events))

    async def test_stale_success_does_not_adopt_isolated_state(self):
        started, attempt = await self.start_attempt()

        async def stale_success(_token):
            attempt.session.cookie_jar.update_cookies(
                {"x_access_token": "stale-secret"}, response_url=URL(attempt.client.base)
            )
            attempt.client.user_id = "stale-user"
            server.qr_generation += 1
            return {"user_id": "stale-user"}

        attempt.client.qr_poll = stale_success
        response = await server.api_login_qr_poll(FakeRequest({
            "token": started["token"], "attempt_id": started["attempt_id"],
        }))

        self.assertEqual(response.status, 409)
        self.assertEqual(json.loads(response.text)["status"], "superseded")
        self.assertEqual(server.client.user_id, "")
        self.assertNotIn(
            "x_access_token", self.main_http.cookie_jar.filter_cookies(URL(server.client.base))
        )

    async def test_stale_runtime_error_returns_superseded_without_event(self):
        started, attempt = await self.start_attempt()

        async def stale_failure(_token):
            server.qr_generation += 1
            raise RuntimeError("old QR expired")

        attempt.client.qr_poll = stale_failure
        response = await server.api_login_qr_poll(FakeRequest({
            "token": started["token"], "attempt_id": started["attempt_id"],
        }))

        self.assertEqual(response.status, 409)
        self.assertEqual(json.loads(response.text)["status"], "superseded")
        self.assertFalse(any(kind == "login" for kind, _ in server.hub.events))

    async def test_successful_poll_cancels_parallel_poll_without_deadlock(self):
        started, attempt = await self.start_attempt()
        blocked = asyncio.Event()
        calls = 0

        async def qr_poll(_token):
            nonlocal calls
            calls += 1
            if calls == 1:
                blocked.set()
                await asyncio.Event().wait()
            attempt.session.cookie_jar.update_cookies(
                {"x_access_token": "winner"}, response_url=URL(attempt.client.base)
            )
            attempt.client.user_id = "winner"
            return {"user_id": "winner"}

        attempt.client.qr_poll = qr_poll
        body = {"token": started["token"], "attempt_id": started["attempt_id"]}
        loser = asyncio.create_task(server.api_login_qr_poll(FakeRequest(body)))
        await blocked.wait()
        winner = asyncio.create_task(server.api_login_qr_poll(FakeRequest(body)))
        winner_response = await asyncio.wait_for(winner, timeout=1)
        loser_result = await asyncio.gather(loser, return_exceptions=True)

        self.assertEqual(winner_response.status, 200)
        self.assertIsInstance(loser_result[0], asyncio.CancelledError)
        self.assertTrue(attempt.session.closed)

    async def test_supersede_returns_its_own_generation_during_concurrency(self):
        release = asyncio.Event()
        blocked = asyncio.Event()

        async def stubborn_task():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                blocked.set()
                await release.wait()

        old_session = aiohttp.ClientSession()
        old_attempt = server.QRAttempt(0, old_session)
        old_task = asyncio.create_task(stubborn_task())
        old_attempt.tasks.add(old_task)
        server.qr_attempt = old_attempt
        await asyncio.sleep(0)

        first = asyncio.create_task(server._supersede_qr_attempt())
        await blocked.wait()
        second_id = await server._supersede_qr_attempt()
        release.set()
        first_id = await first

        self.assertEqual((first_id, second_id), (1, 2))
        self.assertTrue(old_session.closed)

    async def test_logout_invalidates_attempt_started_during_watcher_stop(self):
        started, first_attempt = await self.start_attempt("first-token")
        self.assertEqual(started["attempt_id"], 1)
        raced = {}

        async def stop_with_racing_start():
            result, attempt = await self.start_attempt("racing-token")
            raced.update(result=result, attempt=attempt)

        server.watcher.stop = stop_with_racing_start
        response = await server.api_logout(FakeRequest())

        self.assertEqual(response.status, 200)
        self.assertTrue(first_attempt.session.closed)
        self.assertTrue(raced["attempt"].session.closed)
        self.assertIsNone(server.qr_attempt)
        self.assertEqual(server.client.user_id, "")


class WatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root_patch = mock.patch.object(server, "ROOT", Path(self.temp_directory.name))
        self.root_patch.start()
        self.client = FakeClient()
        self.hub = FakeHub()
        self.watcher = server.Watcher(config(), self.client, ImmediateSolver(), self.hub)

    async def asyncTearDown(self):
        await self.watcher.stop()
        self.root_patch.stop()
        self.temp_directory.cleanup()

    def test_window_uses_real_start_and_safe_submit_point(self):
        received = 1_700_000_005.0
        window = self.watcher._question_window(1_700_000_000_000, 10, received)
        self.assertEqual(window["start"], 1_700_000_000.0)
        self.assertEqual(window["dt_ms"], 1_700_000_000_000)
        self.assertEqual(window["closes_at"], 1_700_000_010.0)
        self.assertEqual(window["submit_by"], 1_700_000_007.5)

    def test_long_question_keeps_server_start_after_delayed_replay(self):
        received = 1_700_000_301.0
        window = self.watcher._question_window(1_700_000_000_000, 600, received)
        self.assertEqual(window["start"], 1_700_000_000.0)
        self.assertEqual(window["closes_at"], 1_700_000_600.0)

    async def test_manual_recheck_never_signs_in(self):
        self.watcher.running = True
        self.watcher.lesson_id = "lesson-1"
        result = await self.watcher.recheck_manual_join()
        self.assertTrue(result["ok"])
        self.assertEqual(self.client.checkin_calls, [("lesson-1", False)])
        self.assertEqual(self.client.bearer, "bearer")
        self.assertEqual(self.client.lesson_token, "lesson")

    async def test_manual_recheck_rejects_stale_lesson_response(self):
        release = asyncio.Event()

        async def delayed_checkin(lesson_id, join_if_not_in=True):
            await release.wait()
            return {"ok": True, "bearer": "old", "lesson_token": "old"}

        self.client.checkin = delayed_checkin
        self.client.bearer = "current"
        self.client.lesson_token = "current"
        self.watcher.running = True
        self.watcher.lesson_id = "lesson-1"
        task = asyncio.create_task(self.watcher.recheck_manual_join())
        await asyncio.sleep(0)
        self.watcher.lesson_id = "lesson-2"
        self.watcher._lesson_generation += 1
        release.set()
        result = await task
        self.assertFalse(result["ok"])
        self.assertEqual(self.watcher._manual_join_ready_for, "")
        self.assertEqual(self.client.bearer, "current")
        self.assertEqual(self.client.lesson_token, "current")

    async def test_manual_join_tolerates_one_empty_lesson_poll(self):
        self.watcher.running = True
        self.watcher.lesson_id = "lesson-1"
        self.client.checkin = mock.AsyncMock(side_effect=[
            {"ok": False, "code": "not_joined"},
            {"ok": True, "bearer": "fresh-bearer", "lesson_token": "fresh-token"},
        ])
        self.client.get_on_lesson = mock.AsyncMock(return_value=[])

        async def skip_wait(awaitable, timeout):
            awaitable.close()

        with mock.patch.object(server.asyncio, "wait_for", new=skip_wait):
            joined = await self.watcher._wait_manual_join("lesson-1", "Course")

        self.assertTrue(joined)
        self.assertEqual(self.client.checkin.await_count, 2)
        self.assertEqual(self.client.bearer, "fresh-bearer")
        self.assertEqual(self.client.lesson_token, "fresh-token")

    async def test_manual_join_stops_after_two_confirmed_empty_lesson_polls(self):
        self.watcher.running = True
        self.watcher.lesson_id = "lesson-1"
        self.client.checkin = mock.AsyncMock(return_value={
            "ok": False, "code": "not_joined",
        })
        self.client.get_on_lesson = mock.AsyncMock(return_value=[])

        async def skip_wait(awaitable, timeout):
            awaitable.close()

        with mock.patch.object(server.asyncio, "wait_for", new=skip_wait):
            joined = await self.watcher._wait_manual_join("lesson-1", "Course")

        self.assertFalse(joined)
        self.assertEqual(self.client.checkin.await_count, 2)
        self.assertEqual(self.client.get_on_lesson.await_count, 2)

    async def test_stop_cancels_pending_answer_before_submission(self):
        self.watcher.solver = BlockingSolver()
        self.watcher.running = True
        self.watcher.lesson_id = "lesson-1"
        self.watcher.problems["p1"] = {
            "problem_id": "p1", "type": 1, "title": "Question", "options": [("A", "One")],
        }
        self.watcher.question_windows["p1"] = {
            "start": time.time(), "dt_ms": int(time.time() * 1000), "limit": 30,
            "safety": 5, "closes_at": time.time() + 30, "submit_by": time.time() + 25,
            "lesson_id": "lesson-1",
        }
        task = asyncio.create_task(self.watcher._answer_flow("p1"))
        self.watcher.answer_tasks["p1"] = task
        await asyncio.sleep(0)
        await self.watcher.stop()
        self.assertTrue(task.cancelled())
        self.assertEqual(self.client.submissions, [])

    async def test_extendtime_updates_both_deadlines(self):
        self.watcher.question_windows["p1"] = {
            "limit": 10, "closes_at": 100, "submit_by": 98,
        }
        self.watcher._extend_problem({"prob": "p1", "extend": 5})
        await asyncio.sleep(0)
        self.assertEqual(self.watcher.question_windows["p1"]["limit"], 15)
        self.assertEqual(self.watcher.question_windows["p1"]["closes_at"], 105)
        self.assertEqual(self.watcher.question_windows["p1"]["submit_by"], 103)

    async def test_leave_lesson_is_sent_before_websocket_context_closes(self):
        class Message:
            type = aiohttp.WSMsgType.TEXT
            data = json.dumps({"op": "lessonfinished", "event": {"code": "LESSON_FINISH"}})

        class Socket:
            def __init__(self):
                self.closed = False
                self.sent = []
                self.messages = [Message()]

            async def send_str(self, payload):
                self.sent.append(json.loads(payload))

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.messages:
                    return self.messages.pop(0)
                raise StopAsyncIteration

        socket = Socket()

        class SocketContext:
            async def __aenter__(self):
                return socket

            async def __aexit__(self, exc_type, exc, traceback):
                socket.closed = True

        class HTTP:
            @staticmethod
            def ws_connect(*args, **kwargs):
                return SocketContext()

        self.client.http = HTTP()
        self.client._headers = lambda: {}
        self.watcher.running = True
        self.watcher.lesson_id = "lesson-1"

        reason = await self.watcher._in_class("lesson-1", "Course")

        self.assertEqual(reason, "finished")
        self.assertTrue(socket.closed)
        self.assertEqual([item["op"] for item in socket.sent], ["hello", "leavelesson"])

    async def test_dry_run_completes_without_upstream_submission(self):
        self.watcher.running = True
        self.watcher.lesson_id = "lesson-1"
        self.watcher.problems["p1"] = {
            "problem_id": "p1", "type": 1, "title": "Question", "options": [("A", "One")],
        }
        now = time.time()
        self.watcher.question_windows["p1"] = {
            "start": now, "dt_ms": int(now * 1000), "limit": 10,
            "safety": 2, "closes_at": now + 10, "submit_by": now + 8,
            "lesson_id": "lesson-1",
        }
        task = asyncio.create_task(self.watcher._answer_flow("p1"))
        self.watcher.answer_tasks["p1"] = task
        await task
        self.assertIn("p1", self.watcher.answered)
        self.assertEqual(self.client.submissions, [])

    async def test_server_logs_problem_receipt_and_result_without_frontend_rendering(self):
        self.watcher.running = True
        self.watcher.lesson_id = "lesson-1"
        self.watcher.problems["logged-problem"] = {
            "problem_id": "logged-problem",
            "type": 1,
            "title": "Question",
            "options": [("A", "One"), ("B", "Two")],
        }

        self.watcher._schedule_problem({
            "prob": "logged-problem",
            "dt": int(time.time() * 1000),
            "limit": 10,
        })
        task = self.watcher.answer_tasks["logged-problem"]
        await task

        messages = [text for _, text in self.hub.logs]
        self.assertTrue(any("收到题目 logged-problem" in text for text in messages))
        self.assertTrue(any("[DRY] logged-problem" in text for text in messages))
        self.assertIn("logged-problem", self.watcher.answered)

    async def test_image_solver_failure_is_shown_but_never_guessed_or_submitted(self):
        image_url = "https://cdn.example.edu/slides/question.jpg"
        self.watcher.cfg["bot"]["dry_run"] = False
        self.watcher.cfg["llm"]["vision_enabled"] = True
        self.watcher.solver = mock.MagicMock()
        self.watcher.solver.solve = mock.AsyncMock(side_effect=RuntimeError("vision unavailable"))
        self.watcher.running = True
        self.watcher.lesson_id = "lesson-1"
        self.watcher.problems["image-problem"] = {
            "problem_id": "image-problem",
            "type": 1,
            "title": "Choose from the diagram",
            "options": [("A", "One"), ("B", "Two")],
            "images": [image_url],
        }
        now = time.time()
        self.watcher.question_windows["image-problem"] = {
            "start": now,
            "dt_ms": int(now * 1000),
            "limit": 10,
            "safety": 2,
            "closes_at": now + 10,
            "submit_by": now + 8,
            "lesson_id": "lesson-1",
        }

        with mock.patch.object(core, "fallback_result", wraps=core.fallback_result) as fallback:
            task = asyncio.create_task(self.watcher._answer_flow("image-problem"))
            self.watcher.answer_tasks["image-problem"] = task
            await task

        fallback.assert_not_called()
        self.watcher.solver.solve.assert_awaited_once_with(self.watcher.problems["image-problem"])
        self.assertEqual(self.client.submissions, [])
        self.assertNotIn("image-problem", self.watcher.answered)
        problem_events = [data for kind, data in self.hub.events if kind == "problem"]
        solving = next(data for data in problem_events if data.get("status") == "solving")
        self.assertEqual(solving["images"], [image_url])
        self.assertFalse(any("兜底" in str(data.get("answer") or "") for data in problem_events))

    async def test_text_solver_failures_are_never_guessed_or_submitted(self):
        self.watcher.cfg["bot"]["dry_run"] = False
        self.watcher.running = True
        self.watcher.lesson_id = "lesson-1"
        failures = (
            RuntimeError("provider unavailable"),
            asyncio.TimeoutError(),
            ValueError("invalid answer JSON"),
            None,
        )

        with mock.patch.object(core, "fallback_result", wraps=core.fallback_result) as fallback:
            for index, failure in enumerate(failures):
                problem_id = f"text-failure-{index}"
                self.watcher.problems[problem_id] = {
                    "problem_id": problem_id,
                    "type": 1,
                    "title": "Question",
                    "options": [("A", "One"), ("B", "Two")],
                    "images": [],
                }
                now = time.time()
                self.watcher.question_windows[problem_id] = {
                    "start": now,
                    "dt_ms": int(now * 1000),
                    "limit": 10,
                    "safety": 2,
                    "closes_at": now + 10,
                    "submit_by": now + 8,
                    "lesson_id": "lesson-1",
                }
                self.watcher.solver = mock.MagicMock()
                if failure is None:
                    self.watcher.solver.solve = mock.AsyncMock(return_value=None)
                else:
                    self.watcher.solver.solve = mock.AsyncMock(side_effect=failure)

                task = asyncio.create_task(self.watcher._answer_flow(problem_id))
                self.watcher.answer_tasks[problem_id] = task
                await task

        fallback.assert_not_called()
        self.assertEqual(self.client.submissions, [])
        self.assertFalse(self.watcher.answered)
        failed = [
            data for kind, data in self.hub.events
            if kind == "problem" and data.get("status") == "failed(solve)"
        ]
        self.assertEqual(len(failed), len(failures))
        self.assertTrue(all(data.get("answer") == "未生成答案" for data in failed))

    async def test_new_presentation_is_fetched_once_before_answering(self):
        calls = 0

        async def fetch_presentation(presentation_id):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return {"slides": [{"problem": {
                "prob": "p1", "type": 1, "title": "Question",
                "options": [{"key": "A", "value": "One"}],
            }}]}

        self.client.fetch_presentation = fetch_presentation
        self.watcher.running = True
        self.watcher.lesson_id = "lesson-1"
        await self.watcher._on_event({
            "op": "unlockproblem", "presentation": "pres-1",
            "problem": {"prob": "p1", "dt": int(time.time() * 1000), "limit": 10},
        })
        await asyncio.gather(*list(self.watcher.answer_tasks.values()))
        if self.watcher.background_tasks:
            await asyncio.gather(*list(self.watcher.background_tasks))
        self.assertEqual(calls, 1)
        self.assertIn("p1", self.watcher.answered)

    async def test_unlockproblem_reuses_presentation_from_hello(self):
        fetch_started = asyncio.Event()
        release_fetch = asyncio.Event()
        calls = []

        async def fetch_presentation(presentation_id):
            calls.append(str(presentation_id))
            fetch_started.set()
            await release_fetch.wait()
            return {"slides": [{"problem": {
                "prob": "p1", "type": 1, "title": "Question",
                "options": [{"key": "A", "value": "One"}],
            }}]}

        self.client.fetch_presentation = fetch_presentation
        self.watcher.running = True
        self.watcher.lesson_id = "lesson-1"
        await self.watcher._on_event({"op": "hello", "presentation": "pres-1"})
        await self.watcher._on_event({
            "op": "unlockproblem",
            "problem": {"prob": "p1", "dt": int(time.time() * 1000), "limit": 10},
        })

        await fetch_started.wait()
        await asyncio.sleep(0)
        self.assertNotIn("p1", self.watcher.answered)
        release_fetch.set()
        await asyncio.gather(*list(self.watcher.answer_tasks.values()))
        if self.watcher.background_tasks:
            await asyncio.gather(*list(self.watcher.background_tasks))

        self.assertEqual(calls, ["pres-1"])
        self.assertIn("p1", self.watcher.answered)
        self.assertEqual(self.client.submissions, [])

    async def test_presentationupdated_prefetches_for_unlock_without_presentation(self):
        calls = []

        async def fetch_presentation(presentation_id):
            calls.append(str(presentation_id))
            return {"slides": [{"problem": {
                "prob": "updated-problem", "type": 1, "title": "Question",
                "options": [{"key": "A", "value": "One"}],
            }}]}

        self.client.fetch_presentation = fetch_presentation
        self.watcher.running = True
        self.watcher.lesson_id = "lesson-1"
        await self.watcher._on_event({
            "op": "presentationupdated", "presentation": "pres-new",
        })
        await self.watcher._on_event({
            "op": "unlockproblem",
            "problem": {
                "prob": "updated-problem",
                "dt": int(time.time() * 1000),
                "limit": 10,
            },
        })

        await asyncio.gather(*list(self.watcher.answer_tasks.values()))
        if self.watcher.background_tasks:
            await asyncio.gather(*list(self.watcher.background_tasks))

        self.assertEqual(calls, ["pres-new"])
        self.assertEqual(self.watcher.active_pres_id, "pres-new")
        self.assertEqual(self.watcher.current_pres, "pres-new")
        self.assertEqual(
            self.watcher.question_windows["updated-problem"]["presentation_id"],
            "pres-new",
        )
        self.assertIn("updated-problem", self.watcher.answered)

    async def test_late_old_force_load_does_not_replace_active_presentation(self):
        old_started = asyncio.Event()
        release_old = asyncio.Event()
        calls = []

        async def fetch_presentation(presentation_id):
            presentation_id = str(presentation_id)
            calls.append(presentation_id)
            if presentation_id == "pres-old":
                old_started.set()
                await release_old.wait()
            return {"slides": [{"problem": {
                "prob": f"problem-{presentation_id}",
                "type": 1,
                "title": "Question",
                "options": [{"key": "A", "value": "One"}],
            }}]}

        self.client.fetch_presentation = fetch_presentation
        self.watcher.running = True
        self.watcher.lesson_id = "lesson-1"
        self.watcher.active_pres_id = "pres-old"
        self.watcher.current_pres = "pres-old"
        self.watcher._presentation_cache.add("pres-old")

        old_load = asyncio.create_task(
            self.watcher._load_presentation("pres-old", force=True)
        )
        await old_started.wait()
        await self.watcher._on_event({
            "op": "presentationupdated", "presentation": "pres-new",
        })
        await self.watcher._load_presentation("pres-new")
        self.assertEqual(self.watcher.current_pres, "pres-new")

        release_old.set()
        await old_load

        self.assertEqual(calls, ["pres-old", "pres-new"])
        self.assertEqual(self.watcher.active_pres_id, "pres-new")
        self.assertEqual(self.watcher.current_pres, "pres-new")
        self.assertIn("problem-pres-old", self.watcher.problems)
        self.assertIn("problem-pres-new", self.watcher.problems)


class PersistenceAndFrontendTests(unittest.TestCase):
    def test_missing_config_uses_safe_public_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "config.json"
            with mock.patch.object(server, "CONFIG_PATH", missing):
                loaded = server.load_config()

        self.assertTrue(loaded["bot"]["dry_run"])
        self.assertFalse(loaded["bot"]["auto_start_watching"])
        self.assertEqual(loaded["llm"]["api_key"], "")
        self.assertTrue(loaded["bot"]["wait_manual_checkin"])

    def test_session_is_written_atomically(self):
        class SessionClient:
            server_key = "yuketang"
            user_id = "u1"
            user_name = "Tester"

            @staticmethod
            def get_cookies():
                return {"x_access_token": "secret"}

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            with mock.patch.object(server, "SESSION_PATH", path):
                self.assertTrue(server.save_session(SessionClient()))
                saved = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(saved["user_id"], "u1")
                self.assertEqual(saved["cookies"]["x_access_token"], "secret")
                self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_frontend_uses_serial_qr_polling(self):
        html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("new AbortController()", html)
        self.assertIn("while (qrController === controller", html)
        self.assertIn("attempt_id:result.attempt_id", html)
        self.assertIn("qrController !== controller", html)
        self.assertIn("message.attempt_id != null && message.attempt_id !== qrAttemptId", html)
        self.assertNotIn("message.attempt_id != null && qrAttemptId != null", html)
        self.assertNotIn("setInterval(async", html)
        self.assertNotIn("btnScan", html)
        self.assertIn('poll.status === "refresh"', html)
        self.assertIn("if (restartQr) void qrStart()", html)
        self.assertIn("请使用微信扫码登录", html)

    def test_manual_join_action_only_appears_for_waiting_account(self):
        html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")

        self.assertNotIn('id="btnJoin"', html)
        self.assertIn('account.phase === "waiting_checkin"', html)
        self.assertIn('actionButton("检测签到", "join", "primary")', html)

    def test_frontend_renders_accounts_without_problem_display_state(self):
        html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="accountList"', html)
        self.assertIn("let accountStates = new Map()", html)
        self.assertIn("row.dataset.accountId = accountId", html)
        self.assertIn('encodeURIComponent(accountId) + "/watch/" + action', html)
        self.assertIn('encodeURIComponent(accountId) + "/remove"', html)
        for removed in (
            'id="problemList"',
            'id="activeProblemCount"',
            "let problemViews = new Map()",
            "let timings = new Map()",
            "function problemView(",
            "function renderProblemImages(",
            "function renderProblemDetails(",
            "function showProblem(",
            "function showAnswer(",
            "function updateProblemTiming(",
        ):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, html)

    def test_frontend_ignores_problem_events_to_avoid_duplicate_logs(self):
        html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
        start = html.index("function connectWs() {")
        end = html.index("async function refreshState()", start)
        websocket_handler = html[start:end]

        self.assertNotIn('message.kind === "problem"', websocket_handler)
        self.assertNotIn('message.kind === "problem_timing"', websocket_handler)

    def test_auto_answer_checkbox_uses_positive_submission_semantics(self):
        html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="autoAnswer"> 自动答题并提交', html)
        self.assertIn('$("autoAnswer").checked = !!state.auto_answer', html)
        self.assertIn("body:JSON.stringify({auto_answer:enabled})", html)
        self.assertIn("开启后会自动选择并向雨课堂提交答案", html)
        self.assertNotIn('id="dryRun"', html)

    def test_frontend_llm_form_keeps_key_private_and_preserves_edits(self):
        html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="llmForm"', html)
        self.assertIn('id="llmBaseUrl" type="url"', html)
        self.assertIn('id="llmApiKey" type="password"', html)
        self.assertIn('<select id="llmModel"', html)
        self.assertIn('id="btnSaveLlm"', html)
        self.assertIn('id="btnFetchModels"', html)
        self.assertIn("if (!llmFormInitialized || !llmFormDirty)", html)
        self.assertIn("if (apiKey) llm.api_key = apiKey", html)
        self.assertIn("body:JSON.stringify({llm})", html)
        self.assertIn('requestJSON("/api/config/llm/models"', html)
        self.assertIn("setModelOptions(result.models, previous)", html)
        self.assertIn('if ($("llmModel").value !== previous) $("llmVision").checked = false', html)
        self.assertIn('$("llmVision").checked = false', html)
        self.assertIn('setModelOptions([], "")', html)
        self.assertIn("for (const control of controls) control.disabled = true", html)
        self.assertIn("if (apiKey) payload.api_key = apiKey;\n  llmFormDirty = true", html)
        self.assertIn('$("btnSaveLlm").disabled = !llmModels.length', html)
        self.assertIn("llmFormDirty = false", html)
        self.assertNotIn('keyInput.value = settings.api_key', html)


class ConfigApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        names = ("cfg", "hub", "client", "solver", "watcher", "llm_session",
                 "config_revision")
        self.originals = {name: getattr(server, name) for name in names}
        server.cfg = config()
        server.cfg["llm"]["api_key"] = "old-secret"
        server.hub = FakeHub()
        server.client = FakeClient()
        server.llm_session = mock.MagicMock()
        server.solver = core.LLMSolver(server.cfg, server.llm_session)

        class ConfigWatcher:
            running = True

            def __init__(self, solver):
                self.solver = solver

        server.watcher = ConfigWatcher(server.solver)
        server.config_revision = 0

    async def asyncTearDown(self):
        for name, value in self.originals.items():
            setattr(server, name, value)

    async def test_get_config_returns_only_sanitized_llm_state(self):
        response = await server.api_config_get(FakeRequest())
        payload = json.loads(response.text)

        self.assertEqual(set(payload["llm"]), {
            "base_url", "model", "vision_enabled", "api_key_configured",
        })
        self.assertTrue(payload["llm"]["api_key_configured"])
        self.assertFalse(payload["llm"]["vision_enabled"])
        self.assertFalse(server.public_state()["llm"]["vision_enabled"])
        self.assertNotIn("old-secret", response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_auto_answer_maps_to_inverse_dry_run(self):
        with mock.patch.object(server, "save_config") as save:
            response = await server.api_config(FakeRequest({"auto_answer": True}))

        self.assertFalse(server.cfg["bot"]["dry_run"])
        self.assertFalse(save.call_args.args[0]["bot"]["dry_run"])
        self.assertIn("自动答题 => 已开启", server.hub.logs[-1][1])
        self.assertEqual(json.loads(response.text)["revision"], 1)
        current = json.loads((await server.api_config_get(FakeRequest())).text)
        self.assertTrue(current["auto_answer"])
        self.assertFalse(current["dry_run"])

    async def test_auto_answer_rejects_conflicting_legacy_flag(self):
        before = json.loads(json.dumps(server.cfg))
        with mock.patch.object(server, "save_config") as save:
            with self.assertRaisesRegex(ValueError, "不能同时设置"):
                await server.api_config(FakeRequest({
                    "auto_answer": True, "dry_run": True,
                }))

        self.assertEqual(server.cfg, before)
        save.assert_not_called()

    async def test_post_llm_config_persists_and_swaps_solver_without_leaking_key(self):
        old_solver = server.solver
        request = FakeRequest({"llm": {
            "base_url": "https://api.example.com/v1/",
            "api_key": "new-top-secret",
            "model": "model-next",
            "vision_enabled": True,
        }})

        with mock.patch.object(server, "save_config") as save:
            response = await server.api_config(request)

        payload = json.loads(response.text)
        persisted = save.call_args.args[0]
        self.assertEqual(persisted["llm"]["api_key"], "new-top-secret")
        self.assertEqual(persisted["llm"]["temperature"], 0.1)
        self.assertTrue(persisted["llm"]["vision_enabled"])
        self.assertIsNot(server.solver, old_solver)
        self.assertIs(server.watcher.solver, server.solver)
        self.assertEqual(server.solver.base, "https://api.example.com/v1/chat/completions")
        self.assertEqual(payload["llm"], {
            "base_url": "https://api.example.com/v1",
            "model": "model-next",
            "vision_enabled": True,
            "api_key_configured": True,
        })
        self.assertNotIn("new-top-secret", response.text)
        self.assertFalse(any("new-top-secret" in text for _, text in server.hub.logs))
        self.assertEqual(payload["revision"], 1)

    async def test_blank_llm_key_preserves_existing_secret(self):
        request = FakeRequest({"llm": {
            "base_url": "https://example.invalid/v1/chat/completions",
            "api_key": "   ",
            "model": "model-next",
        }})

        with mock.patch.object(server, "save_config") as save:
            response = await server.api_config(request)

        self.assertEqual(save.call_args.args[0]["llm"]["api_key"], "old-secret")
        self.assertNotIn("old-secret", response.text)
        self.assertTrue(json.loads(response.text)["llm"]["api_key_configured"])

    async def test_changing_api_url_requires_a_new_key(self):
        before = json.loads(json.dumps(server.cfg))
        old_solver = server.solver
        request = FakeRequest({"llm": {
            "base_url": "https://other.example.com/v1",
            "api_key": "",
            "model": "model-next",
        }})

        with mock.patch.object(server, "save_config") as save:
            with self.assertRaisesRegex(ValueError, "必须填写对应的新 API Key"):
                await server.api_config(request)

        self.assertEqual(server.cfg, before)
        self.assertIs(server.solver, old_solver)
        save.assert_not_called()

    async def test_model_list_uses_unsaved_key_without_returning_it(self):
        response = mock.MagicMock()
        response.__aenter__ = mock.AsyncMock(return_value=response)
        response.__aexit__ = mock.AsyncMock(return_value=None)
        response.status = 200
        response.json = mock.AsyncMock(return_value={"models": [
            {"name": "model-z"}, {"name": "model-a"},
        ]})
        server.llm_session.get.return_value = response
        request = FakeRequest({
            "base_url": "https://new.example.com/v1",
            "api_key": "unsaved-secret",
        })

        result = await server.api_llm_models(request)

        self.assertEqual(json.loads(result.text), {
            "ok": True, "models": ["model-a", "model-z"],
        })
        self.assertNotIn("unsaved-secret", result.text)
        headers = server.llm_session.get.call_args.kwargs["headers"]
        self.assertEqual(headers["authorization"], "Bearer unsaved-secret")

    async def test_model_list_does_not_send_saved_key_to_a_different_api(self):
        response = mock.MagicMock()
        response.__aenter__ = mock.AsyncMock(return_value=response)
        response.__aexit__ = mock.AsyncMock(return_value=None)
        response.status = 200
        response.json = mock.AsyncMock(return_value={"data": [{"id": "local-model"}]})
        server.llm_session.get.return_value = response

        result = await server.api_llm_models(FakeRequest({
            "base_url": "https://new.example.com/v1",
        }))

        self.assertEqual(json.loads(result.text)["models"], ["local-model"])
        self.assertNotIn("authorization", server.llm_session.get.call_args.kwargs["headers"])

    async def test_invalid_llm_config_does_not_change_runtime(self):
        invalid_updates = (
            {"base_url": "http://api.example.com/v1", "model": "model"},
            {"base_url": "https://api.example.com/v1?key=x", "model": "model"},
            {"base_url": "https://api.example.com/v1", "model": ""},
            {"base_url": "https://api.example.com/v1", "model": "model", "api_key": "bad\nkey"},
            {"base_url": "https://api.example.com/v1", "model": "model", "extra": True},
        )
        for update in invalid_updates:
            with self.subTest(update=update):
                before = json.loads(json.dumps(server.cfg))
                old_solver = server.solver
                with mock.patch.object(server, "save_config") as save:
                    with self.assertRaises(ValueError):
                        await server.api_config(FakeRequest({"llm": update}))
                self.assertEqual(server.cfg, before)
                self.assertIs(server.solver, old_solver)
                save.assert_not_called()

    async def test_vision_enabled_must_be_boolean(self):
        before = json.loads(json.dumps(server.cfg))
        request = FakeRequest({"llm": {
            "base_url": "https://example.invalid/v1",
            "model": "test",
            "vision_enabled": "yes",
        }})

        with mock.patch.object(server, "save_config") as save:
            with self.assertRaisesRegex(ValueError, "vision_enabled.*布尔"):
                await server.api_config(request)

        self.assertEqual(server.cfg, before)
        save.assert_not_called()

    async def test_save_failure_does_not_change_runtime(self):
        before = json.loads(json.dumps(server.cfg))
        old_solver = server.solver
        request = FakeRequest({"llm": {
            "base_url": "https://api.example.com/v1",
            "api_key": "new-secret",
            "model": "model-next",
        }})

        with mock.patch.object(server, "save_config", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await server.api_config(request)

        self.assertEqual(server.cfg, before)
        self.assertIs(server.solver, old_solver)
        self.assertIs(server.watcher.solver, old_solver)

    async def test_config_write_requires_same_origin_json(self):
        wrong_type = await server.api_config(FakeRequest(
            {}, content_type="text/plain",
        ))
        wrong_origin = await server.api_config(FakeRequest(
            {}, headers={"Origin": "https://attacker.example"},
        ))

        self.assertEqual(wrong_type.status, 415)
        self.assertEqual(wrong_origin.status, 403)


class MultiAccountPersistenceTests(unittest.TestCase):
    def test_v1_session_migrates_to_v2_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            path.write_text(json.dumps({
                "version": 1,
                "server": "changjiang",
                "user_id": "user-1",
                "user_name": "First",
                "cookies": {"sessionid": "session-1", "sid": "sid-1"},
                "saved_at": 123,
            }), encoding="utf-8")

            records = server.load_account_records(path)
            account_id = server.account_id_for("changjiang", "user-1")
            self.assertEqual(set(records), {account_id})
            self.assertEqual(records[account_id]["cookies"]["sid"], "sid-1")

            server.save_account_records(records, path)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["version"], 2)
            self.assertEqual(set(saved["accounts"]), {account_id})
            self.assertFalse(list(path.parent.glob(".*.tmp")))

    def test_legacy_single_cookie_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            path.write_text(json.dumps({
                "server": "yuketang", "user_id": "user-2", "cookie": "access-2",
            }), encoding="utf-8")

            records = server.load_account_records(path)

            record = next(iter(records.values()))
            self.assertEqual(record["cookies"], {"x_access_token": "access-2"})


class MultiAccountManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        names = ("account_manager", "http_session", "client", "watcher", "SESSION_PATH")
        self.originals = {name: getattr(server, name) for name in names}
        self.tempdir = tempfile.TemporaryDirectory()
        server.SESSION_PATH = Path(self.tempdir.name) / "session.json"
        self.cfg = config()
        self.cfg["server"] = "changjiang"
        self.cfg["bot"]["auto_start_watching"] = False
        self.panel = FakePanel()
        self.solver = core.LLMSolver(self.cfg, object())
        self.manager = server.AccountManager(self.cfg, self.solver, self.panel)
        server.account_manager = self.manager
        self.source_sessions = []

    async def asyncTearDown(self):
        await self.manager.shutdown()
        for session in self.source_sessions:
            if not session.closed:
                await session.close()
        self.tempdir.cleanup()
        for name, value in self.originals.items():
            setattr(server, name, value)

    async def source_client(self, user_id, cookie_value):
        session = aiohttp.ClientSession()
        self.source_sessions.append(session)
        client = core.YuketangClient(self.cfg, session)
        session.cookie_jar.update_cookies(
            {"sessionid": cookie_value, "sid": "sid-" + cookie_value},
            response_url=URL(client.base),
        )
        client.user_id = str(user_id)
        client.user_name = "User " + str(user_id)
        return client

    async def test_two_accounts_use_isolated_sessions_and_duplicate_does_not_replace(self):
        first, created_first = await self.manager.add_verified(
            await self.source_client("1", "cookie-one")
        )
        second, created_second = await self.manager.add_verified(
            await self.source_client("2", "cookie-two")
        )
        duplicate, created_duplicate = await self.manager.add_verified(
            await self.source_client("1", "replacement-cookie")
        )

        self.assertTrue(created_first)
        self.assertTrue(created_second)
        self.assertFalse(created_duplicate)
        self.assertIs(duplicate, first)
        self.assertEqual(len(self.manager.accounts), 2)
        self.assertIsNot(first.session, second.session)
        first_cookies = first.session.cookie_jar.filter_cookies(URL(first.client.base))
        second_cookies = second.session.cookie_jar.filter_cookies(URL(second.client.base))
        self.assertEqual(first_cookies["sessionid"].value, "cookie-one")
        self.assertEqual(second_cookies["sessionid"].value, "cookie-two")
        saved = json.loads(server.SESSION_PATH.read_text(encoding="utf-8"))
        self.assertEqual(saved["version"], 2)
        self.assertEqual(len(saved["accounts"]), 2)

    async def test_remove_one_account_preserves_the_other(self):
        first, _ = await self.manager.add_verified(await self.source_client("1", "one"))
        second, _ = await self.manager.add_verified(await self.source_client("2", "two"))

        removed = await self.manager.remove(first.account_id)

        self.assertTrue(removed)
        self.assertTrue(first.session.closed)
        self.assertFalse(second.session.closed)
        self.assertEqual(set(self.manager.accounts), {second.account_id})
        saved = json.loads(server.SESSION_PATH.read_text(encoding="utf-8"))
        self.assertEqual(set(saved["accounts"]), {second.account_id})

    async def test_restore_two_accounts_uses_distinct_cookie_jars(self):
        records = {}
        for user_id, cookie in (("1", "restored-one"), ("2", "restored-two")):
            account_id = server.account_id_for("changjiang", user_id)
            records[account_id] = {
                "account_id": account_id,
                "server": "changjiang",
                "user_id": user_id,
                "user_name": "Restored " + user_id,
                "cookies": {"sessionid": cookie, "sid": "sid-" + cookie},
                "saved_at": 123,
            }
        self.manager.records = records

        async def restore(instance, saved):
            instance.http.cookie_jar.update_cookies(
                saved["cookies"], response_url=URL(instance.base),
            )
            instance.user_id = saved["user_id"]
            instance.user_name = saved["user_name"]
            return True

        with mock.patch.object(core.YuketangClient, "restore_session", new=restore):
            await self.manager.restore_all()

        self.assertEqual(len(self.manager.accounts), 2)
        runtimes = list(self.manager.accounts.values())
        self.assertIsNot(runtimes[0].session, runtimes[1].session)
        cookie_values = {
            runtime.session.cookie_jar.filter_cookies(URL(runtime.client.base))["sessionid"].value
            for runtime in runtimes
        }
        self.assertEqual(cookie_values, {"restored-one", "restored-two"})

    async def test_shared_solver_update_reaches_every_watcher(self):
        first, _ = await self.manager.add_verified(await self.source_client("1", "one"))
        second, _ = await self.manager.add_verified(await self.source_client("2", "two"))
        next_cfg = config()
        next_cfg["llm"]["base_url"] = "https://next.example/v1"
        replacement = core.LLMSolver(next_cfg, object())

        self.manager.set_solver(replacement)

        self.assertIs(first.watcher.solver, replacement)
        self.assertIs(second.watcher.solver, replacement)

    async def test_public_state_contains_sanitized_account_list(self):
        first, _ = await self.manager.add_verified(await self.source_client("1", "one"))
        second, _ = await self.manager.add_verified(await self.source_client("2", "two"))
        first.hub.state.update({"phase": "in_class", "problems": 3, "answered": 2})
        second.hub.state.update({"phase": "waiting", "problems": 1, "answered": 0})

        state = server.public_state()
        encoded = json.dumps(state)

        self.assertEqual(state["summary"]["total"], 2)
        self.assertEqual(state["problems"], 4)
        self.assertEqual(state["answered"], 2)
        self.assertEqual(len(state["accounts"]), 2)
        self.assertNotIn("cookie-one", encoded)
        self.assertNotIn("cookie-two", encoded)
        self.assertNotIn("cookies", encoded)

    async def test_account_hubs_keep_same_problem_id_isolated(self):
        first_hub = server.AccountHub(self.panel, "account-one", "One")
        second_hub = server.AccountHub(self.panel, "account-two", "Two")

        await first_hub.push("problem", problem_id="shared", status="solving", title="First")
        await second_hub.push("problem", problem_id="shared", status="solving", title="Second")
        await first_hub.push("problem_timing", problem_id="shared", deadline=100)

        self.assertEqual(first_hub.current_problem["title"], "First")
        self.assertEqual(first_hub.current_problem["deadline"], 100)
        self.assertEqual(second_hub.current_problem["title"], "Second")
        self.assertNotIn("deadline", second_hub.current_problem)
        self.assertEqual(
            [event[0] for event in self.panel.events[-3:]],
            ["account-one", "account-two", "account-one"],
        )


class RestoreLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_restore_waits_for_task_cancellation(self):
        started = asyncio.Event()

        async def restore():
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(restore())
        app = {"restore_task": task}
        await started.wait()
        await server._cancel_restore(app)
        self.assertTrue(task.cancelled())


if __name__ == "__main__":
    unittest.main()
