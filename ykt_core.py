# -*- coding: utf-8 -*-
"""雨课堂核心库: HTTP 客户端 / LLM 解题 / 题目解析 / RSA 加密

接口规格来源: 官方 APK (com.xuetangx.ykt) Dart AOT 快照
认证链路: checkin -> Set-Auth 头(bearer) + data.lessonToken(WS 鉴权)
"""
import asyncio
import base64
import binascii
import html
import json
import re
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import aiohttp
from yarl import URL

SERVERS = {
    "yuketang": "https://www.yuketang.cn",
    "pro": "https://pro.yuketang.cn",
    "changjiang": "https://changjiang.yuketang.cn",
    "huanghe": "https://huanghe.yuketang.cn",
}


class AuthenticationExpired(RuntimeError):
    """The upstream explicitly rejected account or classroom credentials."""


class QRCodeScanError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


BASE_HEADERS = {
    "user-agent": "Android",
    "brand": "google Pixel 9 Pro",
    "uuid": "",
    "buildnumber": "1610",
    "xtua": "client=app&tag=1.3.3&platform=Android",
    "systemversion": "16",
    "incremental": "14624737",
    "accept": "application/json",
    "isphysicaldevice": "true",
    "xtbz": "ykt",
    "x-client": "app",
    "content-type": "application/json",
}

PROBLEM_TYPE_NAME = {1: "单选题", 2: "多选题", 3: "投票题", 4: "填空题", 5: "主观题", 6: "判断题"}
MAX_PROBLEM_IMAGES = 8
MAX_INLINE_IMAGE_BYTES = 4 * 1024 * 1024
MAX_INLINE_IMAGE_TOTAL_BYTES = 12 * 1024 * 1024
TRUSTED_IMAGE_HOST_SUFFIXES = ("yuketang.cn", "xuetangx.com")
VISUAL_REFERENCE_RE = re.compile(
    r"(?:如|见|看|读|观察|根据|结合)(?:下|上)?(?:图|表)|"
    r"(?:下|上)(?:图|表)|图中|图片|图像|图示|示意图|流程图|曲线|谱图|显微图|照片|"
    r"坐标图|结构式|谱线|如下所示|该(?:仪器|装置|结构|曲线)|(?:仪器|装置).*(?:读数|示数)|"
    r"figure|diagram|image|graph|chart|pictured|shown\s+(?:below|above)",
    flags=re.I,
)

RSA_PUBLIC_B64 = ("MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCQBaPX7crEH6/jS4hRD7lZrsFRId"
                  "fwEhH30onFnrnWxiRATzP9WEneXJEZHopmzudkNS5bDp51SCnBUGGgfL/sUUrlrhV2x"
                  "nTSe1jRl924ejV5rkVkiii85jp9G8eJrJN6klHs0PfYfp4EVJ8688qpi5iETtg+q4IT"
                  "ocyEyD1+7wIDAQAB")


def rsa_encrypt(plain: str) -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    key = serialization.load_der_public_key(base64.b64decode(RSA_PUBLIC_B64))
    return base64.b64encode(key.encrypt(plain.encode(), padding.PKCS1v15())).decode()


def _plain_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    value = html.unescape(str(value))
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def validate_checkin_qr_url(value: str) -> str:
    value = str(value or "").strip()
    if not value or len(value) > 8192:
        raise ValueError("二维码内容无效")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("二维码 URL 无效") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    expected_path = "/api/v3/lesson/check-in/dynamic-qr-code"
    if (parsed.scheme != "https" or not host.endswith(".yuketang.cn")
            or parsed.username or parsed.password or port is not None
            or not (parsed.path == expected_path
                    or parsed.path.startswith(expected_path + "/"))
            or parsed.fragment):
        raise ValueError("仅支持雨课堂动态签到二维码")
    return value


def _normalize_image_source(value) -> str:
    """Accept image sources that an OpenAI-compatible vision endpoint can fetch."""
    if not isinstance(value, str):
        return ""
    value = html.unescape(value).strip()
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    if value.startswith("data:image/"):
        match = re.fullmatch(
            r"data:image/(png|jpe?g|webp|gif);base64,([A-Za-z0-9+/=\s]+)",
            value,
            flags=re.I,
        )
        if not match:
            return ""
        encoded = re.sub(r"\s+", "", match.group(2))
        if len(encoded) > 5_000_000:
            return ""
        try:
            base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            return ""
        return f"data:image/{match.group(1).lower()};base64,{encoded}"
    if len(value) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return ""
    return value


def _trusted_classroom_image_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    host = (parsed.hostname or "").casefold().rstrip(".")
    return (parsed.scheme == "https" and not parsed.username and not parsed.password
            and port in (None, 443) and not parsed.fragment
            and any(host == suffix or host.endswith("." + suffix)
                    for suffix in TRUSTED_IMAGE_HOST_SUFFIXES))


def _image_mime_type(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _data_url_size(value: str) -> int:
    if not isinstance(value, str) or not value.startswith("data:image/") or "," not in value:
        return 0
    encoded = value.split(",", 1)[1]
    padding = len(encoded) - len(encoded.rstrip("="))
    return max(0, len(encoded) * 3 // 4 - padding)


def _safe_upstream_error(value, *secrets) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = str(value or "")
    text = re.sub(
        r"data:image/[a-z0-9.+-]+;base64,[a-z0-9+/=\s]+",
        "data:image/[redacted]",
        text,
        flags=re.I,
    )
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "sk-[redacted]", text)
    for secret in secrets:
        secret = str(secret or "")
        if len(secret) >= 4:
            text = text.replace(secret, "[redacted]")

    def redact_url(match):
        raw = match.group(0)
        try:
            parsed = urlsplit(raw)
        except ValueError:
            return "[redacted-url]"
        if parsed.query or parsed.fragment:
            return parsed._replace(query="[redacted]", fragment="").geturl()
        return raw

    text = re.sub(r"https?://[^\s\"'<>]+", redact_url, text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:400]


def _llm_error_detail(data, api_key: str) -> str:
    error = data.get("error") if isinstance(data, dict) else data
    if isinstance(error, dict):
        parts = []
        for key in ("message", "type", "param", "code"):
            value = error.get(key)
            if value not in (None, ""):
                cleaned = _safe_upstream_error(value, api_key)
                limit = 240 if key == "message" else 60
                parts.append(f"{key}={cleaned[:limit]}")
        return "; ".join(parts)[:400] if parts else _safe_upstream_error(error, api_key)
    elif not error and isinstance(data, dict):
        error = data.get("message") or data.get("msg")
    return _safe_upstream_error(error, api_key)


class _ImageHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag.casefold() != "img":
            return
        values = {str(key).casefold(): value for key, value in attrs}
        for key in ("src", "data-src", "data-original"):
            source = _normalize_image_source(values.get(key))
            if source:
                self.sources.append(source)
                return


def _html_image_sources(value) -> list[str]:
    if not isinstance(value, str) or "<img" not in value.casefold():
        return []
    parser = _ImageHTMLParser()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        return []
    return parser.sources


def _image_value_sources(value, depth=0) -> list[str]:
    if depth > 3:
        return []
    if isinstance(value, str):
        source = _normalize_image_source(value)
        return [source] if source else _html_image_sources(value)
    if isinstance(value, list):
        sources = []
        for item in value:
            sources.extend(_image_value_sources(item, depth + 1))
        return sources
    if isinstance(value, dict):
        sources = []
        for key in ("url", "src", "pic", "image", "image_url", "data_url", "thumb"):
            if key in value:
                sources.extend(_image_value_sources(value[key], depth + 1))
        return sources
    return []


def _merge_image_items(*groups) -> list[dict]:
    items = []
    by_source = {}
    for group in groups:
        for value in group or ():
            if isinstance(value, dict):
                source = _normalize_image_source(value.get("url"))
                label = _plain_text(value.get("label")) or "题目图片"
                kind = _plain_text(value.get("kind")) or "question_image"
                visual_evidence = bool(value.get("visual_evidence", False))
                text_only_shapes = bool(value.get("text_only_shapes", False))
                try:
                    priority = int(value.get("priority", 10))
                except (TypeError, ValueError):
                    priority = 10
            else:
                source = _normalize_image_source(value)
                label, kind, priority = "题目图片", "question_image", 10
                visual_evidence = False
                text_only_shapes = False
            if not source:
                continue
            existing = by_source.get(source)
            if existing is not None:
                if priority < existing["priority"]:
                    existing.update(label=label, kind=kind, priority=priority)
                existing["visual_evidence"] = (
                    existing.get("visual_evidence", False) or visual_evidence
                )
                existing["text_only_shapes"] = (
                    existing.get("text_only_shapes", False) or text_only_shapes
                )
                continue
            item = {"url": source, "label": label, "kind": kind, "priority": priority,
                    "visual_evidence": visual_evidence, "text_only_shapes": text_only_shapes}
            by_source[source] = item
            items.append(item)
    return items


def _select_image_items(items) -> list[dict]:
    merged = _merge_image_items(items)
    if len(merged) > MAX_PROBLEM_IMAGES:
        selected = sorted(merged, key=lambda item: item["priority"])[:MAX_PROBLEM_IMAGES]
        selected_ids = {id(item) for item in selected}
        merged = [item for item in merged if id(item) in selected_ids]
    selected = []
    inline_bytes = 0
    for item in merged:
        size = _data_url_size(item["url"])
        if size and inline_bytes + size > MAX_INLINE_IMAGE_TOTAL_BYTES:
            continue
        inline_bytes += size
        selected.append({
            "url": item["url"], "label": item["label"], "kind": item["kind"],
            "priority": item["priority"],
            "visual_evidence": item.get("visual_evidence", False),
            "text_only_shapes": item.get("text_only_shapes", False),
        })
    return selected


def _merge_image_sources(*groups) -> list[str]:
    return [item["url"] for item in _select_image_items(
        _merge_image_items(*groups)
    )]


def _slide_has_visual_content(value: dict) -> bool:
    shapes = value.get("shapes")
    if not isinstance(shapes, list):
        return False
    semantic_types = {3, 7, 10, 16, 19, 20, 21, 24, 26, 28, 29}
    picture_types = {11, 13}
    geometric_types = {1, 5, 6, 9}
    pending = list(shapes)
    while pending:
        shape = pending.pop()
        if not isinstance(shape, dict):
            continue
        for key in ("GroupItems", "groupItems", "Shapes", "shapes", "items"):
            children = shape.get(key)
            if isinstance(children, list):
                pending.extend(children)
        try:
            shape_type = int(shape.get("PPTShapeType"))
        except (TypeError, ValueError):
            continue
        if shape_type in semantic_types:
            return True
        if shape_type in picture_types or shape_type in geometric_types:
            try:
                width = abs(float(shape.get("Width") or 0))
                height = abs(float(shape.get("Height") or 0))
            except (TypeError, ValueError):
                width = height = 0
            area = width * height
            if area >= 10_000 or (shape_type == 9 and max(width, height) >= 100):
                return True
    return False


def _slide_has_only_text_shapes(value: dict) -> bool:
    shapes = value.get("shapes")
    if not isinstance(shapes, list) or not shapes:
        return False
    found = False
    for shape in shapes:
        if not isinstance(shape, dict) or shape.get("PPTShapeType") is None:
            return False
        try:
            if int(shape["PPTShapeType"]) != 17:
                return False
        except (TypeError, ValueError):
            return False
        found = True
    return found


def _direct_image_items(value: dict) -> list[dict]:
    items = []
    # A problem slide's full-size cover contains diagrams that are not present in its text fields.
    for key in ("cover", "coverAlt", "thumbnail"):
        candidates = _image_value_sources(value.get(key))
        if candidates:
            items.append({"url": candidates[0], "label": "题目课件页",
                          "kind": "slide_cover", "priority": 30,
                          "visual_evidence": _slide_has_visual_content(value),
                          "text_only_shapes": _slide_has_only_text_shapes(value)})
            break
    for key in ("images", "image", "imageUrl", "image_url", "pictures", "pics"):
        items.extend({"url": source, "label": "题目图片",
                      "kind": "question_image", "priority": 10}
                     for source in _image_value_sources(value.get(key)))
    for key in ("body", "title", "content", "stem"):
        items.extend({"url": source, "label": "题干图片",
                      "kind": "stem_image", "priority": 5}
                     for source in _html_image_sources(value.get(key)))
    raw_options = value.get("options") or value.get("choices") or []
    if isinstance(raw_options, dict):
        raw_options = list(raw_options.values())
    if isinstance(raw_options, list):
        for index, option in enumerate(raw_options):
            default_key = chr(ord("A") + index) if index < 26 else str(index + 1)
            if isinstance(option, dict):
                option_key = _plain_text(
                    option.get("key") or option.get("index") or option.get("label")
                ) or default_key
                for key in ("value", "content", "text"):
                    items.extend({"url": source, "label": f"选项 {option_key} 图片",
                                  "kind": "option_image", "priority": 0}
                                 for source in _image_value_sources(option.get(key)))
                for key in ("images", "image", "imageUrl", "image_url", "pictures", "pics"):
                    items.extend({"url": source, "label": f"选项 {option_key} 图片",
                                  "kind": "option_image", "priority": 0}
                                 for source in _image_value_sources(option.get(key)))
            else:
                items.extend({"url": source, "label": f"选项 {default_key} 图片",
                              "kind": "option_image", "priority": 0}
                             for source in _image_value_sources(option))
    return _merge_image_items(items)


def _problem_needs_slide_cover(problem: dict, image_items=()) -> bool:
    if any(item.get("kind") == "slide_cover" and item.get("visual_evidence")
           for item in image_items if isinstance(item, dict)):
        return True
    title = _plain_text(
        problem.get("body") or problem.get("title")
        or problem.get("content") or problem.get("stem")
    )
    if not title:
        return True
    try:
        problem_type = int(
            problem.get("type") or problem.get("problemType")
            or problem.get("problem_type") or 0
        )
    except (TypeError, ValueError):
        problem_type = 0
    raw_options = problem.get("options") or problem.get("choices") or []
    if isinstance(raw_options, dict):
        raw_options = list(raw_options.values())
    if problem_type in (1, 2, 3, 6) and not raw_options:
        return True
    option_texts = []
    if isinstance(raw_options, list):
        for option in raw_options:
            if isinstance(option, dict):
                text = _plain_text(
                    option.get("value") or option.get("content") or option.get("text")
                )
            else:
                text = _plain_text(option)
            if problem_type in (1, 2, 3, 6) and not text:
                return True
            option_texts.append(text)
    if any(item.get("kind") == "slide_cover" and item.get("text_only_shapes")
           for item in image_items if isinstance(item, dict)):
        return False
    return bool(VISUAL_REFERENCE_RE.search(" ".join([title, *option_texts])))


def qr_scan_app(*values) -> str:
    """Infer which client should scan a login QR code from its provider host."""
    for value in values:
        try:
            host = (urlsplit(str(value or "").strip()).hostname or "").lower()
        except ValueError:
            continue
        if host == "weixin.qq.com" or host.endswith(".weixin.qq.com"):
            return "wechat"
    return "rainclassroom"


def _problem_candidates(value, inherited_image_items=()):
    """Walk only presentation container keys, avoiding unrelated metadata dictionaries."""
    if isinstance(value, list):
        for item in value:
            yield from _problem_candidates(item, inherited_image_items)
        return
    if not isinstance(value, dict):
        return
    image_items = _merge_image_items(inherited_image_items, _direct_image_items(value))
    if any(value.get(k) is not None for k in ("prob", "problemId", "problem_id")):
        yield value, image_items
    problem = value.get("problem")
    if isinstance(problem, dict):
        yield from _problem_candidates(problem, image_items)
    for key in ("slides", "pages", "items", "problems"):
        child = value.get(key)
        if isinstance(child, dict):
            child = list(child.values())
        if isinstance(child, list):
            yield from _problem_candidates(child, image_items)


def extract_problems(presentation_data: dict) -> dict:
    """从 presentation/fetch 响应抽取 {problem_id: problem}, 字段名多候选容错。"""
    if not isinstance(presentation_data, dict):
        return {}
    if isinstance(presentation_data.get("data"), dict):
        presentation_data = presentation_data["data"]
    problems = {}
    for p, candidate_image_items in _problem_candidates(presentation_data):
        pid = str(p.get("prob") or p.get("problemId") or p.get("problem_id") or "").strip()
        if not pid:
            continue
        try:
            ptype = int(p.get("type") or p.get("problemType") or p.get("problem_type") or 0)
        except (TypeError, ValueError):
            ptype = 0
        title = _plain_text(p.get("body") or p.get("title") or p.get("content") or p.get("stem"))
        raw_options = p.get("options") or p.get("choices") or []
        if isinstance(raw_options, dict):
            raw_options = list(raw_options.values())
        options = []
        for index, option in enumerate(raw_options):
            default_key = chr(ord("A") + index) if index < 26 else str(index + 1)
            if isinstance(option, dict):
                key = _plain_text(option.get("key") or option.get("index") or option.get("label")) or default_key
                val = _plain_text(option.get("value") or option.get("content") or option.get("text"))
            else:
                key, val = default_key, _plain_text(option)
            options.append((key, val))
        if not _problem_needs_slide_cover(p, candidate_image_items):
            candidate_image_items = [
                item for item in candidate_image_items
                if item.get("kind") != "slide_cover"
            ]
        image_items = _select_image_items(candidate_image_items)
        problems[pid] = {
            "problem_id": pid,
            "type": ptype,
            "title": title,
            "options": options,
            "images": [item["url"] for item in image_items],
            "image_items": image_items,
            "dt": p.get("dt") or 0,
            "raw": p,
        }
    return problems


SYSTEM_PROMPT = ("你是大学课堂随堂测答题助手。你会收到一道题目，必须只输出一个严格的 JSON 对象，"
                 "不要输出任何解释、markdown 代码块标记或其他文字。")

TYPE_INSTRUCTION = {
    1: '单选题：从选项中选 1 个，输出 {"answers": ["A"]}（方括号里放一个选项字母）',
    2: '多选题：选出所有正确选项，输出 {"answers": ["A", "C"]}（按选项字母）',
    3: '投票题：按题干要求选，输出 {"answers": ["A"]}',
    4: '填空题：题目有几个空就输出几个字符串，按顺序，输出 {"answers": ["第一空", "第二空"]}',
    5: '主观题：给出简洁准确的简答，输出 {"content": "你的回答"}',
    6: '判断题：输出 {"answers": ["A"]}（A=正确/对, B=错误/错, 以选项列表为准）',
}

BLANK_PLACEHOLDER_RE = re.compile(
    r"(?:\[\s*填空(?:\s*\d+)?\s*\]|【\s*填空(?:\s*\d+)?\s*】)"
)


def build_prompt(prob: dict) -> str:
    lines = [f"题型：{PROBLEM_TYPE_NAME.get(prob['type'], prob['type'])}",
             TYPE_INSTRUCTION.get(prob["type"], '输出 {"answers": [...]}'),
             f"题干：{prob['title']}"]
    if prob["options"]:
        lines.append("选项：")
        for k, v in prob["options"]:
            lines.append(f"  {k}. {v}")
    if _problem_image_items(prob):
        lines.append("附图：请结合随消息提供的题目课件图片作答。")
    return "\n".join(lines)


def _problem_image_items(prob: dict) -> list[dict]:
    configured = prob.get("image_items")
    if isinstance(configured, list) and configured:
        return _select_image_items(configured)
    items = []
    for index, source in enumerate(prob.get("images") or []):
        items.append({"url": source, "label": f"题目图片 {index + 1}",
                      "kind": "question_image", "priority": 10})
    return _select_image_items(items)


def build_user_content(prob: dict):
    prompt = build_prompt(prob)
    image_items = _problem_image_items(prob)
    if not image_items:
        return prompt
    content = [{"type": "text", "text": prompt}]
    for item in image_items:
        content.append({"type": "text", "text": item["label"] + "："})
        content.append({"type": "image_url", "image_url": {"url": item["url"]}})
    return content


def fallback_result(problem):
    """LLM 失败/超时时生成符合题型结构的兜底答案。"""
    if isinstance(problem, dict):
        ptype = problem.get("type", 0)
        options = problem.get("options") or []
        first_key = str(options[0][0]) if options else "A"
        blank_count = max(1, len(re.findall(r"\[填空\d*\]", str(problem.get("title") or ""))))
    else:
        ptype, first_key, blank_count = int(problem), "A", 1
    if ptype == 5:
        return {"content": "", "pics": [{"pic": "", "thumb": ""}], "videos": []}
    if ptype == 4:
        return [""] * blank_count
    return [first_key]


def normalize_qr_image(value: str, base_url: str = "") -> str:
    """Return a browser-safe image source for URL, data URI, or raw Base64 input."""
    value = str(value or "").strip()
    if not value:
        return ""
    if value.startswith("data:image/"):
        return value
    if value.startswith(("https://", "http://")):
        return value
    if value.startswith("/") and base_url:
        return urljoin(base_url + "/", value)
    compact = re.sub(r"\s+", "", value)
    try:
        raw = base64.b64decode(compact, validate=True)
    except (ValueError, binascii.Error):
        return ""
    mime = "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif raw.startswith((b"GIF87a", b"GIF89a")):
        mime = "image/gif"
    elif not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return ""
    return f"data:{mime};base64,{compact}"


def chat_completions_url(value: str) -> str:
    """Accept either an OpenAI-compatible base URL or the full chat endpoint."""
    raw = str(value or "").strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise ValueError("模型 API 地址无效") from exc
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("模型 API 地址必须是 http(s) 绝对地址，且不能包含认证信息、查询参数或片段")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("远程模型 API 必须使用 HTTPS；HTTP 仅允许本机地址")
    if parsed.path.rstrip("/").endswith("/chat/completions"):
        return raw
    return raw + "/chat/completions"


def api_root_url(value: str) -> str:
    return chat_completions_url(value).removesuffix("/chat/completions")


def models_url(value: str) -> str:
    return api_root_url(value) + "/models"


def api_key_configured(value: str) -> bool:
    key = str(value or "").strip()
    return bool(key and not key.upper().startswith("YOUR_"))


def parse_llm_json(content) -> dict:
    if isinstance(content, dict):
        nested = content.get("json")
        return nested if isinstance(nested, dict) else content
    if isinstance(content, list):
        parts = []
        objects = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if isinstance(item.get("json"), dict):
                    objects.append(item["json"])
                text = item.get("text") or item.get("output_text")
                if isinstance(text, dict):
                    text = text.get("value")
                if isinstance(text, str):
                    parts.append(text)
        content = "".join(parts)
        if not content.strip() and objects:
            return objects[-1]
    if not isinstance(content, str) or not content.strip():
        raise ValueError("LLM 返回内容为空")
    text = content.strip().lstrip("\ufeff")
    if len(text) > 65_536:
        raise ValueError("LLM 返回内容过长")
    candidates = [text]
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.I | re.S)
    if fenced:
        candidates.append(fenced.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    decoder = json.JSONDecoder()
    decoded = []
    for count, match in enumerate(re.finditer(r"\{", text)):
        if count >= 100:
            break
        try:
            parsed, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            decoded.append(parsed)
    if decoded:
        return decoded[-1]
    raise ValueError("LLM 未返回有效 JSON 对象")


def _answer_strings(parsed: dict) -> list[str]:
    answers = parsed.get("answers")
    if not isinstance(answers, list):
        raise ValueError("LLM 返回的 answers 必须是字符串数组")
    normalized = []
    for answer in answers:
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("LLM 返回的答案包含空值或非字符串值")
        normalized.append(answer.strip())
    if not normalized:
        raise ValueError("LLM 返回的答案为空")
    return normalized


def _validate_choice_answers(prob: dict, answers: list[str]) -> list[str]:
    options = prob.get("options")
    if not isinstance(options, list) or not options:
        raise ValueError("题目没有可校验的选项")
    valid = {}
    for option in options:
        if not isinstance(option, (list, tuple)) or len(option) < 2:
            raise ValueError("题目选项格式无效")
        key = str(option[0]).strip()
        normalized_key = key.casefold()
        if not key or normalized_key in valid:
            raise ValueError("题目选项标识为空或重复")
        valid[normalized_key] = key

    selected = []
    seen = set()
    for answer in answers:
        normalized_answer = answer.casefold()
        if normalized_answer not in valid:
            raise ValueError(f"LLM 返回了非法选项: {answer}")
        if normalized_answer in seen:
            raise ValueError(f"LLM 返回了重复选项: {answer}")
        seen.add(normalized_answer)
        selected.append(valid[normalized_answer])

    if prob["type"] in (1, 3, 6) and len(selected) != 1:
        raise ValueError("单选、投票或判断题必须恰好返回一个选项")
    return selected


def _validate_llm_result(prob: dict, parsed: dict) -> dict:
    problem_type = prob.get("type")
    if problem_type not in TYPE_INSTRUCTION:
        raise ValueError(f"不支持的题型: {problem_type}")
    if problem_type == 5:
        answer = parsed.get("content")
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("LLM 返回了空的主观题答案")
        answer = answer.strip()
        return {
            "result": {
                "content": answer,
                "pics": [{"pic": "", "thumb": ""}],
                "videos": [],
            },
            "display": answer[:120],
        }

    answers = _answer_strings(parsed)
    if problem_type in (1, 2, 3, 6):
        answers = _validate_choice_answers(prob, answers)
    elif problem_type == 4:
        blank_count = len(BLANK_PLACEHOLDER_RE.findall(str(prob.get("title") or "")))
        if blank_count and len(answers) != blank_count:
            raise ValueError(
                f"填空题答案数量不匹配: 题目有 {blank_count} 个空，模型返回 {len(answers)} 个答案"
            )
    return {"result": answers, "display": json.dumps(answers, ensure_ascii=False)}


class LLMSolver:
    def __init__(self, cfg: dict, session: aiohttp.ClientSession):
        self.http = session
        self.configure(cfg["llm"])

    def configure(self, llm_config: dict):
        settings = dict(llm_config)
        self.base = chat_completions_url(settings.get("base_url", ""))
        self.cfg = settings

    async def list_models(self) -> list[str]:
        settings = dict(self.cfg)
        api_key = str(settings.get("api_key") or "").strip()
        headers = {"accept": "application/json"}
        if api_key_configured(api_key):
            headers["authorization"] = f"Bearer {api_key}"
        async with self.http.get(
            models_url(settings.get("base_url", "")),
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
            allow_redirects=False,
        ) as response:
            try:
                payload = await response.json(content_type=None)
            except Exception:
                payload = None
            if response.status != 200:
                raise RuntimeError(f"获取模型列表失败: HTTP {response.status}")

        values = payload if isinstance(payload, list) else None
        if isinstance(payload, dict):
            values = payload.get("data")
        if isinstance(values, dict):
            values = values.get("models") or values.get("data")
        if not isinstance(values, list) and isinstance(payload, dict):
            values = payload.get("models")
        if not isinstance(values, list):
            raise RuntimeError("获取模型列表失败: 响应缺少模型数组")

        models = []
        seen = set()
        for item in values[:2000]:
            if isinstance(item, str):
                model_id = item.strip()
            elif isinstance(item, dict):
                model_id = str(item.get("id") or item.get("name") or item.get("model") or "").strip()
            else:
                continue
            if (not model_id or len(model_id) > 200
                    or any(ord(char) < 32 or ord(char) == 127 for char in model_id)
                    or model_id in seen):
                continue
            seen.add(model_id)
            models.append(model_id)
        if not models:
            raise RuntimeError("获取模型列表失败: 未返回可用模型")
        return sorted(models, key=str.casefold)

    async def _download_image_data_url(self, source: str, byte_limit=None) -> str:
        if not _trusted_classroom_image_url(source):
            return ""
        try:
            byte_limit = min(MAX_INLINE_IMAGE_BYTES, int(
                MAX_INLINE_IMAGE_BYTES if byte_limit is None else byte_limit
            ))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("可信题目图片的大小限制无效") from exc
        if byte_limit <= 0:
            raise RuntimeError("可信题目图片超过内联总大小限制")
        try:
            async with self.http.get(
                source,
                headers={"accept": "image/avif,image/webp,image/png,image/jpeg,image/gif"},
                timeout=aiohttp.ClientTimeout(total=8, connect=3, sock_read=5),
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"下载可信题目图片失败: HTTP {response.status}")
                declared_length = response.headers.get("Content-Length")
                if declared_length is not None:
                    try:
                        declared_length = int(declared_length)
                    except (TypeError, ValueError) as exc:
                        raise RuntimeError("可信题目图片的 Content-Length 无效") from exc
                    if declared_length < 0 or declared_length > byte_limit:
                        raise RuntimeError("可信题目图片超过内联大小限制")
                declared_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
                if declared_type and declared_type not in {
                    "image/jpeg", "image/jpg", "image/png", "image/gif", "image/webp",
                    "application/octet-stream",
                }:
                    raise RuntimeError(f"可信题目图片格式不受支持: {declared_type}")
                chunks = []
                size = 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    if not isinstance(chunk, (bytes, bytearray)):
                        raise RuntimeError("可信题目图片响应包含无效数据")
                    size += len(chunk)
                    if size > byte_limit:
                        raise RuntimeError("可信题目图片超过内联大小限制")
                    chunks.append(bytes(chunk))
        except RuntimeError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, TypeError, ValueError) as exc:
            raise RuntimeError("下载可信题目图片失败") from exc
        data = b"".join(chunks)
        mime = _image_mime_type(data)
        if not mime:
            raise RuntimeError("可信题目图片内容不是受支持的图片格式")
        normalized_declared = "image/jpeg" if declared_type == "image/jpg" else declared_type
        if normalized_declared.startswith("image/") and normalized_declared != mime:
            raise RuntimeError("可信题目图片声明格式与实际内容不一致")
        return f"data:{mime};base64,{base64.b64encode(data).decode()}"

    async def _inline_trusted_images(self, content):
        if not isinstance(content, list):
            return content
        result = []
        inline_bytes = 0
        for block in content:
            copied = dict(block) if isinstance(block, dict) else block
            if isinstance(copied, dict) and copied.get("type") == "image_url":
                image_url = copied.get("image_url")
                if isinstance(image_url, dict):
                    copied["image_url"] = dict(image_url)
                    source = str(image_url.get("url") or "")
                    inline_bytes += _data_url_size(source)
            result.append(copied)
        remaining = max(0, MAX_INLINE_IMAGE_TOTAL_BYTES - inline_bytes)
        for block in result:
            if not isinstance(block, dict) or block.get("type") != "image_url":
                continue
            image_url = block.get("image_url")
            source = str(image_url.get("url") or "") if isinstance(image_url, dict) else ""
            if not _trusted_classroom_image_url(source):
                continue
            if remaining <= 0:
                raise RuntimeError("可信题目图片超过内联总大小限制")
            encoded = await self._download_image_data_url(source, remaining)
            if not encoded:
                raise RuntimeError("可信题目图片无法内联")
            decoded_size = _data_url_size(encoded)
            if not decoded_size or decoded_size > remaining:
                raise RuntimeError("可信题目图片超过内联总大小限制")
            image_url["url"] = encoded
            remaining -= decoded_size
        return result

    async def solve(self, prob: dict) -> dict:
        """返回 {'result': result 字段值, 'display': 日志字符串}"""
        problem_type = prob.get("type")
        if problem_type not in TYPE_INSTRUCTION:
            raise ValueError(f"不支持的题型: {problem_type}")
        settings = dict(self.cfg)
        api_key = str(settings.get("api_key") or "").strip()
        if not api_key_configured(api_key):
            raise RuntimeError("LLM API Key 未配置")
        model = str(settings.get("model") or "").strip()
        if not model:
            raise RuntimeError("LLM 模型未配置")
        if _problem_image_items(prob) and not settings.get("vision_enabled", False):
            raise RuntimeError("当前模型尚未确认支持图片输入")
        base = chat_completions_url(settings.get("base_url", ""))
        original_user_content = build_user_content(prob)
        try:
            user_content = await asyncio.wait_for(
                self._inline_trusted_images(original_user_content), timeout=10
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError("下载可信题目图片超时") from exc
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        }
        headers = {"authorization": f"Bearer {api_key}", "content-type": "application/json"}
        async with self.http.post(base, json=payload, headers=headers,
                                  timeout=aiohttp.ClientTimeout(total=45)) as r:
            try:
                data = await r.json(content_type=None)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                try:
                    response_text = _safe_upstream_error(await r.text(), api_key)
                except Exception:
                    response_text = ""
                suffix = f"：{response_text}" if response_text else ""
                raise RuntimeError(
                    f"LLM HTTP {r.status}: 模型服务返回了非 JSON 响应，请检查 API 地址{suffix}"
                ) from exc
            if r.status != 200:
                detail = _llm_error_detail(data, api_key)
                raise RuntimeError(f"LLM HTTP {r.status}" + (f": {detail}" if detail else ""))
            choices = data.get("choices") if isinstance(data, dict) else None
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise RuntimeError("LLM 响应缺少 choices")
            choice = choices[0]
            message = choice.get("message")
            if not isinstance(message, dict):
                raise RuntimeError("LLM 响应缺少 message")
        parsed = None
        parse_error = None
        for content in (message.get("content"), message.get("reasoning_content"),
                        message.get("reasoning"), choice.get("text")):
            try:
                parsed = parse_llm_json(content)
                break
            except ValueError as exc:
                parse_error = exc
        if parsed is None:
            if choice.get("finish_reason") == "length":
                raise ValueError("LLM 输出因长度限制被截断")
            raise parse_error or ValueError("LLM 返回内容为空")
        return _validate_llm_result(prob, parsed)


class YuketangClient:
    def __init__(self, cfg: dict, session: aiohttp.ClientSession):
        self.cfg = cfg
        self.server_key = cfg.get("server", "yuketang")
        self.base = SERVERS.get(self.server_key, SERVERS["yuketang"])
        self.http = session
        self.bearer = ""
        self.lesson_token = ""
        self.user_id = ""
        self.user_name = ""
        self.lesson_discovery = {}

    def _headers(self, with_bearer: bool = True) -> dict:
        h = dict(BASE_HEADERS)
        cookie_jar = getattr(self.http, "cookie_jar", None)
        if cookie_jar is not None:
            cookies = cookie_jar.filter_cookies(URL(self.base))
            if cookies:
                csrf = cookies.get("csrftoken")
                session = cookies.get("sessionid")
                if csrf:
                    h["x-csrftoken"] = csrf.value
                h["x-uid"] = self.user_id
                if session:
                    h["sessionid"] = session.value
        if with_bearer and self.bearer:
            h["authorization"] = f"Bearer {self.bearer}"
        return h

    async def _req(self, method: str, path: str, **kw):
        url = path if path.startswith("http") else self.base + path
        timeout = kw.pop("timeout", aiohttp.ClientTimeout(total=35))
        with_bearer = kw.pop("with_bearer", True)
        async with self.http.request(method, url, headers=self._headers(with_bearer),
                                     timeout=timeout, **kw) as r:
            try:
                data = await r.json(content_type=None)
            except Exception:
                data = None
            return r.status, r.headers, data

    # ---------- 登录 ----------

    async def restore_cookie(self, cookie: str) -> bool:
        cookie = str(cookie or "").strip()
        if not cookie:
            return False
        self.http.cookie_jar.update_cookies({"x_access_token": cookie}, response_url=URL(self.base))
        return await self.check_session()

    async def restore_session(self, saved: dict) -> bool:
        cookies = saved.get("cookies") if isinstance(saved, dict) else None
        has_saved_cookie = False
        if isinstance(cookies, dict):
            clean = {str(k): str(v) for k, v in cookies.items() if k and v}
            if clean:
                self.http.cookie_jar.update_cookies(clean, response_url=URL(self.base))
                has_saved_cookie = True
        legacy = saved.get("cookie") if isinstance(saved, dict) else ""
        if legacy and not has_saved_cookie:
            self.http.cookie_jar.update_cookies(
                {"x_access_token": str(legacy)}, response_url=URL(self.base)
            )
            has_saved_cookie = True
        return has_saved_cookie and await self.check_session()

    async def check_session(self) -> bool:
        for path in ("/api/v3/user/basic-info", "/v/course_meta/user_info"):
            st, _, data = await self._req(
                "GET", path, timeout=aiohttp.ClientTimeout(total=10)
            )
            if st >= 400 or not isinstance(data, dict):
                continue
            d = data.get("data") or data
            if isinstance(d, dict) and isinstance(d.get("user_profile"), dict):
                d = d["user_profile"]
            if not isinstance(d, dict):
                continue
            user_id = d.get("user_id") or d.get("userId") or d.get("id")
            if user_id:
                self.user_id = str(user_id)
                self.user_name = str(d.get("name") or d.get("nickname") or "")
                return True
        self.user_id = ""
        self.user_name = ""
        return False

    async def qr_start(self) -> dict:
        """发起扫码登录: qr_image may be a URL, data URI, or encoded image."""
        st, _, data = await self._req("GET", "/api/v3/user/login/pre-info")
        if st >= 400 or not data or data.get("code") != 0:
            raise RuntimeError(f"获取登录二维码失败: HTTP {st}, code={data.get('code') if data else '?'}")
        d = data.get("data") or {}
        token = str(d.get("token") or "")
        image = normalize_qr_image(d.get("qrImage", ""), self.base)
        if not token or not image:
            raise RuntimeError("登录二维码响应缺少 token 或有效图片")
        content = str(d.get("qrContent") or "")
        return {"token": token, "qr_image": image, "qr_dataurl": image,
                "qr_content": content, "scan_app": qr_scan_app(content, image)}

    async def qr_poll(self, token: str):
        """Poll once, returning a verified user or a signal to replace the QR code."""
        st, _, data = await self._req("POST", "/api/v3/user/login", json={"token": token})
        if st >= 500 or not data:
            raise RuntimeError(f"扫码登录轮询失败: HTTP {st}")
        code = data.get("code")
        if code == 0:
            ok = await self.check_session()
            if not ok:
                raise RuntimeError("扫码成功但会话校验失败")
            return {"user_id": self.user_id}
        if code in (50001, 50400):
            return {
                "status": "refresh",
                "code": code,
                "message": str(data.get("message") or data.get("msg") or "二维码已过期"),
            }
        message = data.get("message") or data.get("msg") or "未知错误"
        raise RuntimeError(f"扫码登录失败 code={code}: {message}")

    async def password_login(self, phone_number: str, password: str):
        body = {"type": 2, "phoneNumber": phone_number, "password": rsa_encrypt(password),
                "email": phone_number if "@" in phone_number else "",
                "pushDeviceId": f"pybot-{int(time.time()*1000)}", "ticket": "", "rand": ""}
        st, _, data = await self._req("POST", "/api/v3/user/login/app", json=body)
        if not data or data.get("code") != 0:
            raise RuntimeError(f"密码登录失败(多半需要人机验证 ticket/rand): {data}")
        if not await self.check_session():
            raise RuntimeError("登录后会话校验失败")
        return True

    def get_cookie(self) -> str:
        for cookie in self.http.cookie_jar:
            if cookie.key == "x_access_token":
                return cookie.value
        return ""

    def get_cookies(self) -> dict:
        return {cookie.key: cookie.value for cookie in self.http.cookie_jar}

    def adopt_login(self, source: "YuketangClient"):
        """Atomically copy a verified login from an isolated Rain Classroom client."""
        if self.server_key != source.server_key or self.base != source.base:
            raise ValueError("二维码登录服务器与当前服务器不一致")
        cookies = {
            name: morsel.value
            for name, morsel in source.http.cookie_jar.filter_cookies(URL(source.base)).items()
        }
        if not source.user_id or not cookies:
            raise ValueError("二维码登录结果缺少用户或会话 Cookie")
        self.clear_session()
        self.http.cookie_jar.update_cookies(cookies, response_url=URL(self.base))
        self.user_id = source.user_id
        self.user_name = source.user_name

    def clear_session(self):
        self.http.cookie_jar.clear()
        self.user_id = ""
        self.user_name = ""
        self.bearer = ""
        self.lesson_token = ""

    # ---------- 课程 ----------

    @staticmethod
    def _normalize_lesson_room(room):
        if not isinstance(room, dict):
            return None
        lesson_id = room.get("lessonId") or room.get("lesson_id")
        if lesson_id is None or str(lesson_id).strip() == "":
            return None
        normalized = dict(room)
        normalized["lessonId"] = str(lesson_id)
        aliases = {
            "courseId": "course_id",
            "courseName": "course_name",
            "classroomId": "classroom_id",
            "classroomName": "classroom_name",
        }
        for target, source in aliases.items():
            if normalized.get(target) is None and room.get(source) is not None:
                normalized[target] = room[source]
        return normalized

    async def get_on_lesson(self):
        """Return active lessons from the endpoint extracted from the official APK."""
        path = "/api/v3/classroom/on-lesson-upcoming-exam"
        retryable_statuses = {502, 503, 504}
        last_error = None
        valid = False

        for attempt in range(2):
            try:
                status, _, payload = await self._req("GET", path)
            except Exception as exc:
                last_error = {"error": type(exc).__name__}
                break

            if status in (401, 403):
                self.lesson_discovery = {"latest": {
                    "ok": False, "http": status, "auth_expired": True,
                }}
                raise AuthenticationExpired(f"雨课堂账号授权已失效: HTTP {status}")

            data = payload.get("data") if isinstance(payload, dict) else None
            values = data.get("onLessonClassrooms") if isinstance(data, dict) else None
            code = payload.get("code") if isinstance(payload, dict) else None
            valid = (status < 400 and isinstance(payload, dict) and code == 0
                     and isinstance(data, dict) and isinstance(values, list))
            if valid:
                break
            last_error = {"http": status, "code": code}
            if attempt == 0 and status in retryable_statuses:
                await asyncio.sleep(1)
                continue
            break

        if not valid:
            latest = {"ok": False, **(last_error or {})}
            self.lesson_discovery = {"latest": latest}
            if "error" in latest:
                raise RuntimeError(f"查询正在上课课程失败: {latest['error']}")
            raise RuntimeError(
                f"查询正在上课课程失败: HTTP {latest.get('http')}, code={latest.get('code')}"
            )

        rooms = [self._normalize_lesson_room(item) for item in values]
        rooms = [item for item in rooms if item]
        upcoming = data.get("upcomingExam")
        self.lesson_discovery = {
            "latest": {
                "ok": True,
                "count": len(rooms),
                "upcoming_exam_count": len(upcoming) if isinstance(upcoming, list) else None,
            }
        }

        deduplicated = []
        seen = set()
        for room in rooms:
            lesson_id = str(room["lessonId"])
            if lesson_id in seen:
                continue
            seen.add(lesson_id)
            deduplicated.append(room)
        return deduplicated

    async def scan_qr(self, url: str):
        url = validate_checkin_qr_url(url)
        st, _, data = await self._req(
            "POST", "/api/v3/app/scan", json={"url": url}, with_bearer=False,
        )
        if st in (401, 403):
            raise AuthenticationExpired(f"雨课堂账号授权已失效: HTTP {st}")
        if not isinstance(data, dict) or data.get("code") != 0:
            code = data.get("code") if isinstance(data, dict) else None
            message = ("动态二维码已过期，请重新扫码"
                       if str(code) == "51203" else "二维码解析失败")
            raise QRCodeScanError(code, message)
        payload = data.get("data")
        value = payload.get("value") if isinstance(payload, dict) else None
        if (not isinstance(payload, dict) or payload.get("type") != "checkin"
                or value is None or not str(value).strip()):
            raise QRCodeScanError(None, "二维码响应缺少有效课堂标识")
        return str(value)

    async def checkin(self, lesson_id: str, join_if_not_in: bool = False):
        """Check or explicitly join a lesson, returning credentials without mutating state."""
        body = {"source": 21, "lessonId": str(lesson_id), "joinIfNotIn": join_if_not_in}
        async with self.http.post(self.base + "/api/v3/lesson/checkin", headers=self._headers(with_bearer=False),
                                  json=body, timeout=aiohttp.ClientTimeout(total=35)) as r:
            status = r.status
            if status in (401, 403):
                raise AuthenticationExpired(f"雨课堂账号授权已失效: HTTP {status}")
            data = await r.json(content_type=None)
        if not data or data.get("code") != 0:
            code = data.get("code") if data else "?"
            msg = {50070: "课堂开启动态二维码签到, 需扫码", 51203: "动态二维码已过期"}.get(code, str(data))
            return {"ok": False, "code": code, "message": msg}
        bearer = r.headers.get("Set-Auth") or r.headers.get("set-auth") or ""
        lesson_token = (data.get("data") or {}).get("lessonToken") or ""
        if not bearer or not lesson_token:
            return {"ok": False, "code": "no_token", "message": "checkin 成功但缺少 bearer/lessonToken"}
        return {"ok": True, "bearer": bearer, "lesson_token": lesson_token}

    # ---------- 课件/题目 ----------

    async def fetch_presentation(self, pres_id):
        st, _, data = await self._req(
            "GET", "/api/v3/lesson/presentation/fetch", params={"presentation_id": str(pres_id)}
        )
        if st in (401, 403):
            raise AuthenticationExpired(f"雨课堂课堂授权已失效: HTTP {st}")
        if data and data.get("code") == 0:
            return data.get("data")
        return None

    async def fetch_answer(self, problem_id):
        st, _, data = await self._req(
            "GET", "/api/v3/lesson/problem/fetch-answer", params={"problem_id": str(problem_id)}
        )
        if st in (401, 403):
            raise AuthenticationExpired(f"雨课堂课堂授权已失效: HTTP {st}")
        return data

    async def submit_answer(self, problem_id, dt, problem_type, result, retry=False, timeout=None):
        path = "/api/v3/lesson/problem/retry" if retry else "/api/v3/lesson/problem/answer"
        problem = {"problemId": str(problem_id), "dt": int(dt),
                   "problemType": int(problem_type), "result": result}
        if retry:
            problem["retry_times"] = None
            body = {"problems": [problem]}
        else:
            body = problem
        request_timeout = aiohttp.ClientTimeout(total=timeout) if timeout else None
        st, _, data = await self._req("POST", path, json=body, **({"timeout": request_timeout} if request_timeout else {}))
        if st in (401, 403):
            raise AuthenticationExpired(f"雨课堂课堂授权已失效: HTTP {st}")
        return data
