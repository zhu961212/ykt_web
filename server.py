# -*- coding: utf-8 -*-
"""雨课堂自动答题本地网站

流程: 扫码登录 -> 自动发现在上课的课程 -> 等待 App 手动签到 -> WS 实时监听发题
      -> LLM 解题 -> 在答题截止时间前提交 -> 下课自动回等待, 直到手动停止

运行:  python server.py   ->  http://127.0.0.1:8765
"""
import asyncio
import copy
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import string
import time
from collections import OrderedDict, deque
from pathlib import Path

import aiohttp
from aiohttp import web

import email_notifier as emailer
import ykt_core as core

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
SESSION_PATH = ROOT / "session.json"

WS_HOST_SUFFIX = {"yuketang": "www.yuketang.cn"}

DEFAULTS = {
    "server": "yuketang",
    "llm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key": "",
        "model": "glm-4-flash",
        "temperature": 0.1,
        "vision_enabled": False,
    },
    "lesson": {"poll_interval": 3},
    "email": dict(emailer.DEFAULT_EMAIL_SETTINGS),
    "bot": {
        "dry_run": True,
        "wait_manual_checkin": True,
        "auto_start_watching": False,
        "answer_delay_seconds": 5,
        "safety_seconds": 8,
        "request_margin_seconds": 1.5,
    },
}


def load_config() -> dict:
    loaded = {}
    if CONFIG_PATH.exists():
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("config.json 必须包含 JSON 对象")
    merged = {**DEFAULTS, **loaded}
    merged["llm"] = {**DEFAULTS["llm"], **loaded.get("llm", {})}
    merged["lesson"] = {**DEFAULTS["lesson"], **loaded.get("lesson", {})}
    merged["email"] = emailer.merge_email_settings(
        DEFAULTS["email"], loaded.get("email", {}),
    )
    merged["bot"] = {**DEFAULTS["bot"], **loaded.get("bot", {})}
    # Background watching never joins a class; scan check-in requires an explicit user action.
    merged["bot"]["wait_manual_checkin"] = True
    return merged


def save_config(cfg: dict):
    _atomic_json_write(CONFIG_PATH, cfg)


def public_llm_config(config=None) -> dict:
    settings = (config or cfg).get("llm", {})
    return {
        "base_url": str(settings.get("base_url") or ""),
        "model": str(settings.get("model") or ""),
        "vision_enabled": bool(settings.get("vision_enabled", False)),
        "api_key_configured": core.api_key_configured(settings.get("api_key", "")),
    }


def public_email_config(config=None) -> dict:
    return emailer.public_email_settings((config or cfg).get("email", {}))


def public_admin_config(request=None, config=None) -> dict:
    runtime = getattr(request, "app", {}).get("runtime_config", {}) if request else {}
    username = runtime.get("username")
    if not username:
        persisted = (config or cfg).get("admin")
        username = persisted.get("username") if isinstance(persisted, dict) else None
    persisted = (config or cfg).get("admin")
    password_required = (bool(runtime.get("auth_enabled")) if runtime
                         else bool(isinstance(persisted, dict)
                                   or os.environ.get("YKT_ADMIN_PASSWORD")))
    return {
        "username": str(username or os.environ.get("YKT_ADMIN_USERNAME") or "admin"),
        "password_required": password_required,
        "reset_mode": bool(runtime.get("admin_reset", False)),
    }


def public_scanner_config(request=None, config=None) -> dict:
    runtime = getattr(request, "app", {}).get("runtime_config", {}) if request else {}
    configured = (bool(runtime.get("scanner_enabled")) if runtime
                  else isinstance((config or cfg).get("scanner"), dict))
    return {"configured": configured, "path": "/scan"}


ADMIN_USERNAME_CHARS = frozenset(string.ascii_letters + string.digits + "-._@")
ADMIN_PASSWORD_CHARS = frozenset(string.ascii_letters + string.digits + "-._~")
ADMIN_PASSWORD_ITERATIONS = 310_000
SCANNER_PASSWORD_ITERATIONS = 210_000


def _validate_admin_username(value):
    value = str(value or "").strip()
    if not value or len(value) > 128 or any(char not in ADMIN_USERNAME_CHARS for char in value):
        raise ValueError("管理员账号只能使用字母、数字及 - . _ @")
    return value


def _validate_admin_password(value, *, allow_empty=False):
    if not isinstance(value, str):
        raise ValueError("管理员密码必须是字符串")
    if allow_empty and not value:
        return value
    if not 12 <= len(value) <= 256 or any(char not in ADMIN_PASSWORD_CHARS for char in value):
        raise ValueError("管理员密码必须为 12-256 位，只能使用字母、数字及 - . _ ~")
    return value


def _validate_scanner_password(value):
    if (not isinstance(value, str) or not 8 <= len(value) <= 128
            or any(char not in ADMIN_PASSWORD_CHARS for char in value)):
        raise ValueError("扫码页密码必须为 8-128 位，只能使用字母、数字及 - . _ ~")
    return value


def _password_digest(password, salt, iterations):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def _persisted_admin_credentials(config):
    value = (config or {}).get("admin")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RuntimeError("config.json 的 admin 配置无效")
    try:
        username = _validate_admin_username(value.get("username"))
        salt = bytes.fromhex(str(value.get("password_salt") or ""))
        password_hash = bytes.fromhex(str(value.get("password_hash") or ""))
        iterations = int(value.get("password_iterations") or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("config.json 的管理员凭据无效") from exc
    if len(salt) != 16 or len(password_hash) != 32 or not 100_000 <= iterations <= 2_000_000:
        raise RuntimeError("config.json 的管理员凭据无效")
    return {
        "username": username,
        "password": "",
        "password_salt": salt,
        "password_hash": password_hash,
        "password_iterations": iterations,
    }


def _persisted_scanner_credentials(config):
    value = (config or {}).get("scanner")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RuntimeError("config.json 的 scanner 配置无效")
    try:
        salt = bytes.fromhex(str(value.get("password_salt") or ""))
        password_hash = bytes.fromhex(str(value.get("password_hash") or ""))
        iterations = int(value.get("password_iterations") or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("config.json 的扫码页凭据无效") from exc
    if len(salt) != 16 or len(password_hash) != 32 or not 100_000 <= iterations <= 2_000_000:
        raise RuntimeError("config.json 的扫码页凭据无效")
    return {
        "scanner_password_salt": salt,
        "scanner_password_hash": password_hash,
        "scanner_password_iterations": iterations,
    }


def server_runtime_config(environ=None, config=None) -> dict:
    environ = os.environ if environ is None else environ
    host = str(environ.get("YKT_HOST") or "127.0.0.1").strip()
    username = str(environ.get("YKT_ADMIN_USERNAME") or "admin")
    password = str(environ.get("YKT_ADMIN_PASSWORD") or "")
    try:
        port = int(environ.get("YKT_PORT") or 8765)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("YKT_PORT 必须是 1-65535 的整数") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("YKT_PORT 必须是 1-65535 的整数")
    if not host or len(host) > 253 or any(char.isspace() for char in host):
        raise RuntimeError("YKT_HOST 无效")
    try:
        address = ipaddress.ip_address(host)
        loopback = address.is_loopback
    except ValueError:
        if host != "localhost":
            raise RuntimeError("YKT_HOST 必须是 IP 地址或 localhost")
        loopback = True
    try:
        username = _validate_admin_username(username)
        password = _validate_admin_password(password, allow_empty=True)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    secure_cookie = str(environ.get("YKT_SECURE_COOKIE") or "").lower()
    if secure_cookie not in ("", "0", "1", "false", "true"):
        raise RuntimeError("YKT_SECURE_COOKIE 必须是 0 或 1")
    trust_proxy = str(environ.get("YKT_TRUST_PROXY") or "").lower()
    if trust_proxy not in ("", "0", "1", "false", "true"):
        raise RuntimeError("YKT_TRUST_PROXY 必须是 0 或 1")
    trust_proxy_enabled = trust_proxy in ("1", "true")
    admin_reset = str(environ.get("YKT_ADMIN_RESET") or "").lower()
    if admin_reset not in ("", "0", "1", "false", "true"):
        raise RuntimeError("YKT_ADMIN_RESET 必须是 0 或 1")
    admin_reset_enabled = admin_reset in ("1", "true")
    persisted = None if admin_reset_enabled else _persisted_admin_credentials(config)
    scanner = _persisted_scanner_credentials(config)
    auth_enabled = bool(password)
    if persisted is not None:
        username = persisted["username"]
        password = ""
        auth_enabled = True
    if (not loopback or trust_proxy_enabled) and not auth_enabled:
        raise RuntimeError("远程访问或反向代理模式必须设置至少 12 位的 YKT_ADMIN_PASSWORD")
    settings = {
        "host": host,
        "port": port,
        "auth_enabled": auth_enabled,
        "username": username,
        "password": password,
        "secure_cookie": secure_cookie in ("1", "true"),
        "trust_proxy": trust_proxy_enabled,
        "admin_reset": admin_reset_enabled,
        "session_secret": secrets.token_bytes(32),
        "scanner_enabled": scanner is not None,
        "scanner_session_secret": secrets.token_bytes(32),
    }
    if persisted is not None:
        settings.update(persisted)
    if scanner is not None:
        settings.update(scanner)
    return settings


def _atomic_json_write(path: Path, value: dict):
    temp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            os.chmod(temp, 0o600)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        temp.unlink(missing_ok=True)


def save_session(client: core.YuketangClient) -> bool:
    payload = {
        "version": 1,
        "server": client.server_key,
        "user_id": client.user_id,
        "user_name": client.user_name,
        "cookies": client.get_cookies(),
        "saved_at": int(time.time()),
    }
    if not payload["cookies"]:
        return False
    _atomic_json_write(SESSION_PATH, payload)
    return True


def account_id_for(server_key: str, user_id: str) -> str:
    identity = f"{server_key}\0{user_id}".encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:16]


def client_session_record(client: core.YuketangClient, account_id=None) -> dict:
    cookies = client.get_cookies()
    if not client.user_id or not cookies:
        raise ValueError("账号会话缺少用户或 Cookie")
    return {
        "account_id": account_id or account_id_for(client.server_key, client.user_id),
        "server": client.server_key,
        "user_id": str(client.user_id),
        "user_name": str(client.user_name or ""),
        "cookies": cookies,
        "saved_at": int(time.time()),
    }


def load_account_records(path=None) -> dict:
    path = path or SESSION_PATH
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and payload.get("version") == 2:
        values = payload.get("accounts", {})
        values = list(values.values()) if isinstance(values, dict) else values
    else:
        values = [payload]
    records = {}
    for value in values if isinstance(values, list) else []:
        if not isinstance(value, dict):
            continue
        server_key = str(value.get("server") or "")
        user_id = str(value.get("user_id") or "")
        cookies = value.get("cookies")
        if not isinstance(cookies, dict):
            legacy_cookie = value.get("cookie")
            cookies = {"x_access_token": str(legacy_cookie)} if legacy_cookie else {}
        cookies = {str(key): str(item) for key, item in cookies.items() if key and item}
        if server_key not in core.SERVERS or not user_id or not cookies:
            continue
        account_id = account_id_for(server_key, user_id)
        record = {
            "account_id": account_id,
            "server": server_key,
            "user_id": user_id,
            "user_name": str(value.get("user_name") or ""),
            "cookies": cookies,
            "saved_at": int(value.get("saved_at") or 0),
        }
        existing = records.get(account_id)
        if existing is None or record["saved_at"] >= existing["saved_at"]:
            records[account_id] = record
    return records


def save_account_records(records: dict, path=None):
    path = path or SESSION_PATH
    payload = {"version": 2, "accounts": records}
    _atomic_json_write(path, payload)


class Hub:
    """把日志/状态/题目事件推给所有连着的浏览器 (WS /ws)"""

    def __init__(self):
        self.clients = set()
        self.history = deque(maxlen=400)
        self.current_problem = None
        self.state = {"phase": "idle", "user_id": "", "user_name": "", "lesson_id": "",
                      "course_name": "", "answered": 0, "problems": 0, "dry_run": True,
                      "session_saved": SESSION_PATH.exists(), "detail": "未启动"}

    async def push(self, kind: str, **data):
        payload = {"kind": kind, "ts": time.time(), **data}
        if kind == "problem":
            if (not self.current_problem or data.get("status") == "solving" or
                    self.current_problem.get("problem_id") != data.get("problem_id")):
                self.current_problem = dict(payload)
            else:
                self.current_problem.update(data)
                self.current_problem["ts"] = payload["ts"]
        elif (kind == "problem_timing" and self.current_problem and
              self.current_problem.get("problem_id") == data.get("problem_id")):
            self.current_problem.update(data)
        msg = json.dumps(payload, ensure_ascii=False)
        self.history.append(msg)
        for ws in list(self.clients):
            try:
                await ws.send_str(msg)
            except Exception:
                self.clients.discard(ws)

    async def push_account(self, account_id: str, kind: str, **data):
        payload = {"kind": kind, "ts": time.time(), "account_id": account_id, **data}
        msg = json.dumps(payload, ensure_ascii=False)
        self.history.append(msg)
        for ws in list(self.clients):
            try:
                await ws.send_str(msg)
            except Exception:
                self.clients.discard(ws)

    def log(self, text: str, level: str = "info"):
        print(time.strftime("[%H:%M:%S] ") + text, flush=True)
        asyncio.ensure_future(self.push("log", text=text, level=level))

    def log_account(self, account_id: str, account_name: str, text: str, level: str = "info"):
        label = account_name or account_id
        print(time.strftime("[%H:%M:%S] ") + f"[{label}] {text}", flush=True)
        asyncio.ensure_future(self.push_account(
            account_id, "log", text=text, level=level, account_name=account_name,
        ))

    async def sync_state(self):
        try:
            payload = public_state()
        except NameError:
            payload = dict(self.state)
        await self.push("state", **payload)

    def clear_problem(self):
        self.current_problem = None


class AccountHub:
    """Per-account state adapter over the shared browser event hub."""

    def __init__(self, panel: Hub, account_id: str, account_name: str):
        self.panel = panel
        self.account_id = account_id
        self.account_name = account_name
        self.current_problem = None
        self.state = {
            "phase": "idle", "lesson_id": "", "course_name": "", "answered": 0,
            "problems": 0, "dry_run": True, "detail": "未启动",
        }

    async def push(self, kind: str, **data):
        payload = {"kind": kind, "ts": time.time(), "account_id": self.account_id, **data}
        if kind == "problem":
            if (not self.current_problem or data.get("status") == "solving" or
                    self.current_problem.get("problem_id") != data.get("problem_id")):
                self.current_problem = dict(payload)
            else:
                self.current_problem.update(data)
                self.current_problem["ts"] = payload["ts"]
        elif (kind == "problem_timing" and self.current_problem and
              self.current_problem.get("problem_id") == data.get("problem_id")):
            self.current_problem.update(data)
        await self.panel.push_account(
            self.account_id, kind, account_name=self.account_name, **data,
        )

    def log(self, text: str, level: str = "info"):
        self.panel.log_account(self.account_id, self.account_name, text, level)

    async def sync_state(self):
        await self.panel.sync_state()

    def clear_problem(self):
        self.current_problem = None


class SharedAnswerPool:
    """Deduplicate LLM work for the same problem across account watchers."""

    def __init__(self, max_completed: int = 512):
        self.max_completed = max(1, int(max_completed))
        self._completed = OrderedDict()
        self._inflight = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(server_key, lesson_id, problem_id):
        return str(server_key), str(lesson_id), str(problem_id)

    @staticmethod
    def _observe_task(task: asyncio.Task):
        if not task.cancelled():
            task.exception()

    async def _remove_inflight(self, key, task):
        async with self._lock:
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)

    async def _run(self, key, problem: dict, solver):
        try:
            result = await solver.solve(problem)
        except asyncio.CancelledError:
            await self._remove_inflight(key, asyncio.current_task())
            raise
        except Exception:
            await self._remove_inflight(key, asyncio.current_task())
            raise

        async with self._lock:
            if self._inflight.get(key) is asyncio.current_task():
                self._inflight.pop(key, None)
                if result is not None:
                    self._completed[key] = copy.deepcopy(result)
                    self._completed.move_to_end(key)
                    while len(self._completed) > self.max_completed:
                        self._completed.popitem(last=False)
        return result

    async def solve(self, server_key, lesson_id, problem_id, problem: dict, solver):
        key = self._key(server_key, lesson_id, problem_id)
        async with self._lock:
            cached = self._completed.get(key)
            if cached is not None:
                self._completed.move_to_end(key)
                return copy.deepcopy(cached)
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._run(key, copy.deepcopy(problem), solver),
                    name=f"shared-answer-{key[1]}-{key[2]}",
                )
                task.add_done_callback(self._observe_task)
                self._inflight[key] = task
        return copy.deepcopy(await asyncio.shield(task))

    async def clear(self):
        async with self._lock:
            tasks = list(self._inflight.values())
            self._inflight.clear()
            self._completed.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class Watcher:
    """waiting -> manual check-in detection -> WebSocket classroom -> waiting."""

    def __init__(self, cfg: dict, client: core.YuketangClient, solver: core.LLMSolver,
                 hub: Hub, answer_pool: SharedAnswerPool = None,
                 notifier: emailer.EmailNotifier = None):
        self.cfg = cfg
        self.client = client
        self.solver = solver
        self.hub = hub
        self.answer_pool = answer_pool or SharedAnswerPool()
        self._owns_answer_pool = answer_pool is None
        self.notifier = notifier
        self.account_id = getattr(hub, "account_id", "single")
        self.running = False
        self.task = None
        self.problems = {}
        self.answered = set()
        self.answer_tasks = {}
        self.background_tasks = set()
        self.question_windows = {}
        self.current_problem_id = ""
        self.current_pres = None
        self.active_pres_id = None
        self._presentation_cache = set()
        self._presentation_inflight = {}
        self.lesson_id = ""
        self.course_name = ""
        self._manual_join_event = asyncio.Event()
        self._manual_join_ready_for = ""
        self._lesson_generation = 0
        self._monitor_failures = 0
        self._ws = None

    def set_state(self, phase: str, detail=None, **values):
        state = self.hub.state
        state["phase"] = phase
        if detail is not None:
            state["detail"] = detail
        state["dry_run"] = self.cfg["bot"].get("dry_run", True)
        state["answered"] = len(self.answered)
        state["problems"] = len(self.problems)
        state.update(values)
        asyncio.create_task(self.hub.sync_state())

    def start(self) -> bool:
        if self.running:
            return False
        self.running = True
        self.task = asyncio.create_task(self._run(), name=f"watcher-{self.account_id}")
        self.set_state("waiting", "正在查询上课课程")
        self.hub.log("监课已启动（等待 App 手动签到或面板扫码签到）")
        return True

    async def stop(self):
        was_running = self.running
        self.running = False
        self._manual_join_event.set()
        task, self.task = self.task, None
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._cancel_class_tasks()
        if self.notifier is not None:
            self.notifier.clear(f"{self.account_id}:monitor")
            self.notifier.clear(f"{self.account_id}:classroom_connection")
        if self._owns_answer_pool:
            await self.answer_pool.clear()
        self._clear_class()
        self.set_state("idle", "已停止", lesson_id="", course_name="")
        if was_running:
            self.hub.log("监课已停止", "warn")

    async def _cancel_class_tasks(self):
        current = asyncio.current_task()
        tasks = [task for task in [*self.answer_tasks.values(), *self.background_tasks]
                 if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.answer_tasks.clear()
        self.background_tasks.clear()
        self.question_windows.clear()
        self.current_problem_id = ""

    def _clear_class(self):
        self.problems.clear()
        self.answered.clear()
        self.current_pres = None
        self.active_pres_id = None
        self._presentation_cache.clear()
        self._presentation_inflight.clear()
        self.hub.clear_problem()
        self.lesson_id = ""
        self.course_name = ""
        self.client.bearer = ""
        self.client.lesson_token = ""
        self._manual_join_ready_for = ""
        self._lesson_generation += 1
        self._manual_join_event.clear()

    async def _run(self):
        poll = max(1.0, float(self.cfg.get("lesson", {}).get("poll_interval", 3)))
        try:
            while self.running:
                rooms = await self.client.get_on_lesson()
                lesson = self._pick_lesson(rooms)
                if not lesson:
                    self._monitor_failures = 0
                    if self.notifier is not None:
                        self.notifier.clear(f"{self.account_id}:monitor")
                    self.set_state("waiting", f"暂无在上课课程，{poll:g}s 后重试",
                                   lesson_id="", course_name="")
                    await asyncio.sleep(poll)
                    continue
                lid, cname = lesson
                if lid != self.lesson_id:
                    self._lesson_generation += 1
                    self.lesson_id, self.course_name = lid, cname
                    self.hub.log(f"发现在上课课程: lessonId={lid} {cname}")
                self.set_state("waiting_checkin", f"已发现 {cname}，等待你在 App 手动签到",
                               lesson_id=lid, course_name=cname)
                if not await self._wait_manual_join(lid, cname):
                    if self.running:
                        self.hub.log(f"课程 {lid} 已结束或离开上课列表", "warn")
                        self._clear_class()
                    continue
                if not self.running:
                    break
                self._monitor_failures = 0
                if self.notifier is not None:
                    self.notifier.clear(f"{self.account_id}:monitor")
                self.hub.log("检测到手动签到完成，开始监听课堂实时事件")
                reason = await self._in_class(lid, cname)
                await self._cancel_class_tasks()
                self._clear_class()
                if self.running:
                    message = "收到下课信号，继续等待下一节课" if reason == "finished" else "课程已结束，继续等待下一节课"
                    self.hub.log(message)
                    self.set_state("waiting", message, lesson_id="", course_name="")
                    await asyncio.sleep(poll)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.running:
                if isinstance(exc, core.AuthenticationExpired):
                    self._notify_issue("authorization", "雨课堂账号授权已失效", str(exc))
                else:
                    self._monitor_failures += 1
                    if self._monitor_failures >= 3:
                        self._notify_issue(
                            "monitor", "雨课堂监课连续异常",
                            f"监课已连续失败 {self._monitor_failures} 次: {exc}",
                        )
                self.hub.log(f"监课循环异常: {exc}", "error")
                self.set_state("error", f"监课异常，5s 后重试: {exc}")
                await asyncio.sleep(5)
                if self.running:
                    self.task = asyncio.create_task(
                        self._run(), name=f"watcher-restart-{self.account_id}"
                    )

    @staticmethod
    def _pick_lesson(rooms):
        for room in rooms or []:
            if not isinstance(room, dict):
                continue
            lesson_id = room.get("lessonId") or room.get("lesson_id")
            if lesson_id:
                name = room.get("courseName") or room.get("course_name") or room.get("courseId") or "当前课程"
                return str(lesson_id), str(name)
        return None

    async def _wait_manual_join(self, lesson_id: str, course_name: str) -> bool:
        poll = max(1.0, float(self.cfg.get("lesson", {}).get("poll_interval", 3)))
        waited = 0.0
        missing_polls = 0
        while self.running and self.lesson_id == lesson_id:
            if self._manual_join_ready_for == lesson_id:
                return True
            result = await self.client.checkin(lesson_id, join_if_not_in=False)
            self._monitor_failures = 0
            if self.notifier is not None:
                self.notifier.clear(f"{self.account_id}:monitor")
            if not self.running or self.lesson_id != lesson_id:
                return False
            if result.get("ok"):
                self.client.bearer = result["bearer"]
                self.client.lesson_token = result["lesson_token"]
                return True
            try:
                rooms = await self.client.get_on_lesson()
                still_active = any(
                    str(item.get("lessonId") or item.get("lesson_id")) == lesson_id
                    for item in rooms if isinstance(item, dict)
                )
                missing_polls = 0 if still_active else missing_polls + 1
                if missing_polls >= 2:
                    return False
            except core.AuthenticationExpired:
                raise
            except Exception as exc:
                self.hub.log(f"确认课程状态失败，将继续等待: {exc}", "warn")
            waited += poll
            self.set_state("waiting_checkin", f"等待你在 App 手动签到（已等待 {waited:.0f}s）",
                           lesson_id=lesson_id, course_name=course_name)
            self._manual_join_event.clear()
            try:
                await asyncio.wait_for(self._manual_join_event.wait(), timeout=poll)
            except asyncio.TimeoutError:
                pass
        return False

    async def recheck_manual_join(self) -> dict:
        if not self.running or not self.lesson_id:
            return {"ok": False, "message": "尚未发现上课课程或监课未启动"}
        lesson_id = self.lesson_id
        generation = self._lesson_generation
        try:
            result = await self.client.checkin(lesson_id, join_if_not_in=False)
        except core.AuthenticationExpired as exc:
            self._notify_issue("authorization", "雨课堂账号授权已失效", str(exc))
            return {"ok": False, "message": "账号授权已失效，请重新登录"}
        if not self.running or self.lesson_id != lesson_id or self._lesson_generation != generation:
            return {"ok": False, "message": "课程状态已变化，请重新检测"}
        if not result.get("ok"):
            return {"ok": False, "message": "暂未检测到签到，请先在雨课堂 App 完成签到"}
        self.client.bearer = result["bearer"]
        self.client.lesson_token = result["lesson_token"]
        self._manual_join_ready_for = lesson_id
        self._manual_join_event.set()
        return {"ok": True, "lesson_id": lesson_id}

    async def _in_class(self, lesson_id: str, course_name: str) -> str:
        host = WS_HOST_SUFFIX.get(self.client.server_key) or self.client.base.removeprefix("https://")
        ws_url = f"wss://{host}/wsapp/"
        self.set_state("in_class", f"课堂中，正在实时监听发题", lesson_id=lesson_id, course_name=course_name)
        failures = 0
        while self.running and self.lesson_id == lesson_id:
            connected_at = None
            try:
                async with self.client.http.ws_connect(
                    ws_url, headers=self.client._headers(), heartbeat=20, timeout=15
                ) as ws:
                    self._ws = ws
                    connected_at = time.monotonic()
                    try:
                        hello = {"op": "hello", "userid": self.client.user_id, "role": "student",
                                 "auth": self.client.lesson_token, "lessonid": lesson_id}
                        await ws.send_str(json.dumps(hello))
                        self.hub.log(f"课堂通道已连接: {ws_url}")
                        async for message in ws:
                            if message.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    event = json.loads(message.data)
                                except (TypeError, json.JSONDecodeError):
                                    continue
                                await self._on_event(event)
                            elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                    finally:
                        # Send while the context still owns an open socket.
                        await self._leave_lesson(lesson_id)
                if not self.running:
                    return "stopped"
                failures = 0 if connected_at and time.monotonic() - connected_at >= 30 else failures + 1
                self.hub.log("课堂通道已断开，正在确认课程状态", "warn")
            except _LessonFinished:
                return "finished"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                self.hub.log(f"课堂通道异常: {exc}", "warn")
                if failures >= 3:
                    self._notify_issue(
                        "classroom_connection", "雨课堂课堂连接连续异常",
                        f"课堂 {lesson_id} 已连续连接失败 {failures} 次: {exc}",
                    )
            finally:
                self._ws = None

            try:
                rooms = await self.client.get_on_lesson()
                if not any(str(item.get("lessonId") or item.get("lesson_id")) == lesson_id
                           for item in rooms if isinstance(item, dict)):
                    return "ended"
                refreshed = await self.client.checkin(lesson_id, join_if_not_in=False)
                if refreshed.get("ok"):
                    self.client.bearer = refreshed["bearer"]
                    self.client.lesson_token = refreshed["lesson_token"]
                    self.hub.log("课堂凭证已刷新，准备重连", "debug")
            except core.AuthenticationExpired as exc:
                self._notify_issue("authorization", "雨课堂账号授权已失效", str(exc))
                raise
            except Exception as exc:
                self.hub.log(f"重连前状态确认失败: {exc}", "warn")
            delay = min(2 ** min(failures, 4), 15)
            self.set_state("reconnecting", f"课堂连接中断，{delay}s 后重连")
            await asyncio.sleep(delay)
            self.set_state("in_class", "课堂中，正在实时监听发题")
        return "stopped"

    async def _leave_lesson(self, lesson_id: str):
        ws = self._ws
        if ws and not ws.closed:
            try:
                await ws.send_str(json.dumps({"op": "leavelesson", "lessonid": lesson_id}))
            except Exception:
                pass

    @staticmethod
    def _presentation_id(value):
        if isinstance(value, dict):
            return value.get("id") or value.get("presentationId") or value.get("presentation_id") or value.get("pres")
        return value

    def _spawn_background(self, coroutine, name: str):
        task = asyncio.create_task(coroutine, name=f"{self.account_id}-{name}")
        self.background_tasks.add(task)
        task.add_done_callback(self._background_done)
        return task

    def _background_done(self, task):
        self.background_tasks.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error:
            self.hub.log(f"后台任务 {task.get_name()} 失败: {error}", "warn")

    def _notify_issue(self, category: str, subject: str, detail: str):
        if self.notifier is None:
            return
        account_name = getattr(self.hub, "account_name", "") or self.client.user_name
        body = (
            "雨课堂答题面板检测到需要处理的问题。\n\n"
            f"账号: {account_name or self.client.user_id or self.account_id}\n"
            f"服务: {self.client.server_key}\n"
            f"课堂: {self.course_name or '-'} ({self.lesson_id or '-'})\n"
            f"详情: {str(detail)[:1000]}\n"
            f"时间: {time.strftime('%Y-%m-%d %H:%M:%S %z')}\n"
        )
        self._spawn_background(
            self.notifier.notify(f"{self.account_id}:{category}", subject, body),
            f"email-{category}",
        )

    async def _on_event(self, event: dict):
        op = event.get("op") or event.get("type") or ""
        if op in ("hello", "showpresentation", "presentationupdated"):
            if op == "hello" and self.notifier is not None:
                self.notifier.clear(f"{self.account_id}:classroom_connection")
            if op != "presentationupdated" and event.get("message") == "lesson finished":
                self.hub.log("服务端通知课堂已结束")
                raise _LessonFinished()
            presentation_id = self._presentation_id(event.get("presentation") or event.get("pres"))
            if presentation_id is not None:
                presentation_id = str(presentation_id)
                self.active_pres_id = presentation_id
                self.current_pres = (
                    presentation_id if presentation_id in self._presentation_cache else None
                )
                self._spawn_background(
                    self._load_presentation(
                        presentation_id, force=op == "presentationupdated"
                    ),
                    f"presentation-{presentation_id}",
                )
            self._restore_timeline(event.get("timeline") or [])
            action = "更新" if op == "presentationupdated" else "同步"
            self.hub.log(f"课件{action} presentation={presentation_id} slide={event.get('slideindex')}")
        elif op == "unlockproblem":
            problem = event.get("problem") or {}
            presentation_id = self._presentation_id(
                event.get("presentation") or event.get("pres") or problem.get("pres")
            )
            if presentation_id is not None:
                presentation_id = str(presentation_id)
                self.active_pres_id = presentation_id
                if presentation_id != str(self.current_pres):
                    self.current_pres = (
                        presentation_id if presentation_id in self._presentation_cache else None
                    )
                    self._spawn_background(
                        self._load_presentation(presentation_id),
                        f"presentation-{presentation_id}",
                    )
            self._schedule_problem(problem, presentation_id)
        elif op == "extendtime":
            self._extend_problem(event.get("problem") or {})
        elif op == "lessonfinished":
            self.hub.log("收到下课信号 lessonfinished")
            raise _LessonFinished()
        elif op not in ("slide", "slidenav", "callpaused", "showfinished"):
            self.hub.log(f"课堂事件 {op}: {json.dumps(event, ensure_ascii=False)[:160]}", "debug")

    def _restore_timeline(self, timeline):
        if not isinstance(timeline, list):
            return
        for item in timeline:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "problem" and item.get("prob"):
                try:
                    raw = float(item.get("dt"))
                    start = raw / 1000.0 if raw > 10_000_000_000 else raw
                    limit = max(1.0, float(item.get("limit") or 30))
                except (TypeError, ValueError):
                    continue
                now = time.time()
                if abs(start - now) <= 86_400 and start + limit > now:
                    self._schedule_problem(item, self._presentation_id(item.get("pres")))

    async def _fetch_presentation(self, presentation_id: str, generation: int) -> bool:
        try:
            data = await self.client.fetch_presentation(presentation_id)
        except asyncio.CancelledError:
            raise
        except core.AuthenticationExpired as exc:
            self._notify_issue("authorization", "雨课堂课堂授权已失效", str(exc))
            self.hub.log(f"课件 {presentation_id} 获取失败: {exc}", "warn")
            return False
        except Exception as exc:
            self.hub.log(f"课件 {presentation_id} 获取失败: {exc}", "warn")
            return False
        if not data:
            self.hub.log(f"课件 {presentation_id} 获取失败", "warn")
            return False
        if generation != self._lesson_generation:
            return False

        found = core.extract_problems(data)
        self.problems.update(found)
        self._presentation_cache.add(presentation_id)
        # A late response for an older presentation may populate its problems, but
        # it must never move the active presentation backwards.
        if self.active_pres_id is None or presentation_id == self.active_pres_id:
            self.current_pres = presentation_id
        try:
            dump = ROOT / "logs" / self.account_id / f"presentation_{presentation_id}.json"
            dump.parent.mkdir(parents=True, exist_ok=True)
            dump.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as exc:
            self.hub.log(f"课件日志保存失败: {exc}", "warn")
        self.hub.log(f"课件 {presentation_id} 已缓存，累计 {len(self.problems)} 题"
                     + ("" if found else "（未解析到题目字段）"),
                     "info" if found else "warn")
        self.set_state("in_class", "课堂中，正在实时监听发题")
        return True

    def _presentation_fetch_done(self, presentation_id: str, task: asyncio.Task):
        if self._presentation_inflight.get(presentation_id) is task:
            self._presentation_inflight.pop(presentation_id, None)
        self._background_done(task)

    async def _load_presentation(self, presentation_id, force=False, required_problem_id=None):
        presentation_id = self._presentation_id(presentation_id)
        if presentation_id is None:
            return False
        presentation_id = str(presentation_id)
        if required_problem_id is not None and str(required_problem_id) in self.problems:
            if self.active_pres_id is None or presentation_id == self.active_pres_id:
                self.current_pres = presentation_id
            return True
        if not force and presentation_id in self._presentation_cache:
            if self.active_pres_id is None or presentation_id == self.active_pres_id:
                self.current_pres = presentation_id
            return True

        task = self._presentation_inflight.get(presentation_id)
        if task is not None and task.done():
            self._presentation_inflight.pop(presentation_id, None)
            task = None
        if task is None:
            task = asyncio.create_task(
                self._fetch_presentation(presentation_id, self._lesson_generation),
                name=f"{self.account_id}-presentation-fetch-{presentation_id}",
            )
            self._presentation_inflight[presentation_id] = task
            self.background_tasks.add(task)
            task.add_done_callback(
                lambda completed, pid=presentation_id: self._presentation_fetch_done(pid, completed)
            )
        return await asyncio.shield(task)

    def _question_window(self, dt_value, limit_value, received_at: float) -> dict:
        try:
            limit = max(1.0, float(limit_value or 30))
        except (TypeError, ValueError):
            limit = 30.0
        start = received_at
        try:
            raw = float(dt_value)
            candidate = raw / 1000.0 if raw > 10_000_000_000 else raw
            if abs(candidate - received_at) <= 86_400:
                start = candidate
        except (TypeError, ValueError):
            pass
        configured_safety = max(0.5, float(self.cfg["bot"].get("safety_seconds", 8)))
        safety = min(configured_safety, max(0.5, limit * 0.25))
        closes_at = start + limit
        submit_by = closes_at - safety
        return {"start": start, "dt_ms": int(start * 1000), "limit": limit,
                "safety": safety, "closes_at": closes_at, "submit_by": submit_by,
                "lesson_id": self.lesson_id}

    def _schedule_problem(self, problem_event: dict, presentation_id=None):
        problem_id = str(problem_event.get("prob") or problem_event.get("problemId") or "").strip()
        if not problem_id or problem_id in self.answered or problem_id in self.answer_tasks:
            return
        window = self._question_window(problem_event.get("dt"), problem_event.get("limit"), time.time())
        if window["submit_by"] <= time.time():
            return
        window["presentation_id"] = self._presentation_id(presentation_id) or self.active_pres_id
        self.question_windows[problem_id] = window
        self.current_problem_id = problem_id
        self.hub.log(f"收到题目 {problem_id}，限时 {window['limit']:g}s，计划至少提前 {window['safety']:g}s 提交")
        task = asyncio.create_task(
            self._answer_flow(problem_id), name=f"answer-{self.account_id}-{problem_id}"
        )
        self.answer_tasks[problem_id] = task

    def _extend_problem(self, problem_event: dict):
        problem_id = str(problem_event.get("prob") or problem_event.get("problemId") or self.current_problem_id)
        try:
            extension = float(problem_event.get("extend") or 0)
        except (TypeError, ValueError):
            extension = 0
        window = self.question_windows.get(problem_id)
        if not window or extension <= 0:
            return
        window["limit"] += extension
        window["closes_at"] += extension
        window["submit_by"] += extension
        self.hub.log(f"题目 {problem_id} 延长 {extension:g}s")
        self._spawn_background(
            self.hub.push("problem_timing", problem_id=problem_id,
                          deadline=window["closes_at"], submit_by=window["submit_by"]),
            f"timing-{problem_id}",
        )

    async def _answer_flow(self, problem_id: str):
        try:
            window = self.question_windows[problem_id]
            if window["lesson_id"] != self.lesson_id:
                return
            problem = self.problems.get(problem_id)
            presentation_id = window.get("presentation_id") or self.current_pres
            if not problem and presentation_id is not None:
                remaining = window["submit_by"] - time.time()
                if remaining > 0.25:
                    try:
                        await asyncio.wait_for(
                            self._load_presentation(
                                presentation_id, force=True, required_problem_id=problem_id,
                            ),
                            timeout=max(0.1, remaining - 0.1),
                        )
                    except asyncio.TimeoutError:
                        pass
                problem = self.problems.get(problem_id)
            if not problem:
                self.hub.log(f"无法取得题目 {problem_id} 的内容，未提交", "error")
                await self.hub.push("problem", problem_id=problem_id, title="题目内容缺失",
                                    status="missed")
                return

            type_name = core.PROBLEM_TYPE_NAME.get(problem["type"], str(problem["type"]))
            await self.hub.push(
                "problem", problem_id=problem_id, title=problem["title"][:240], type=type_name,
                options=[{"key": key, "value": value} for key, value in problem["options"]],
                images=[{"url": item["url"], "alt": item["label"]}
                        for item in problem.get("image_items") or []]
                       or (problem.get("images") or []),
                deadline=window["closes_at"], submit_by=window["submit_by"],
                duration=window["limit"], status="solving",
                dry_run=self.cfg["bot"].get("dry_run", True),
            )

            solved = None
            error = ""
            request_margin = max(0.5, float(self.cfg["bot"].get("request_margin_seconds", 1.5)))
            solve_budget = window["submit_by"] - time.time() - request_margin
            if (problem.get("images")
                    and not self.cfg.get("llm", {}).get("vision_enabled", False)):
                error = "模型尚未确认支持图片输入"
            elif solve_budget > 0.1:
                try:
                    solved = await asyncio.wait_for(
                        self.answer_pool.solve(
                            self.client.server_key, window["lesson_id"], problem_id,
                            problem, self.solver,
                        ),
                        timeout=solve_budget,
                    )
                except asyncio.TimeoutError:
                    error = "LLM 超时"
                except Exception as exc:
                    error = str(exc)
            else:
                error = "剩余时间不足"
            if solved is None:
                error = error or "模型未返回有效答案"
                self.hub.log(f"题目 {problem_id} 解题失败（{error}），为避免误答未提交", "error")
                await self.hub.push(
                    "problem", problem_id=problem_id, status="failed(solve)",
                    answer="未生成答案",
                )
                return

            answer_delay = max(0.0, float(self.cfg["bot"].get("answer_delay_seconds", 5)))
            submit_at = min(window["start"] + answer_delay, window["submit_by"] - 0.1)
            if time.time() < submit_at:
                await asyncio.sleep(submit_at - time.time())
            if not self.running or window["lesson_id"] != self.lesson_id:
                return
            remaining = window["submit_by"] - time.time()
            if remaining <= 0.1:
                self.hub.log(f"题目 {problem_id} 已错过安全提交窗口，未提交", "warn")
                await self.hub.push("problem", problem_id=problem_id, status="expired",
                                    answer=solved["display"])
                return

            dry_run = self.cfg["bot"].get("dry_run", True)
            if dry_run:
                status = "dry"
                self.hub.log(f"[DRY] {problem_id} {type_name} => {solved['display']}")
            else:
                try:
                    response = await self.client.submit_answer(
                        problem_id, window["dt_ms"], problem["type"], solved["result"],
                        timeout=max(0.1, min(10.0, remaining)),
                    )
                    code = response.get("code") if isinstance(response, dict) else "?"
                    status = "submitted" if code == 0 else f"failed({code})"
                    self.hub.log(f"[提交] {problem_id} {type_name} => {solved['display']} code={code}",
                                 "info" if code == 0 else "error")
                except core.AuthenticationExpired as exc:
                    status = "failed(auth)"
                    self._notify_issue("authorization", "雨课堂课堂授权已失效", str(exc))
                    self.hub.log(f"题目 {problem_id} 提交失败: {exc}", "error")
                except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                    status = "failed(timeout)"
                    self.hub.log(f"题目 {problem_id} 提交未在安全窗口内完成: {exc}", "error")
                except Exception as exc:
                    status = "failed(error)"
                    self.hub.log(f"题目 {problem_id} 提交失败: {exc}", "error")
            if status in ("dry", "submitted"):
                self.answered.add(problem_id)
                self.set_state("in_class", "课堂中，正在实时监听发题")
            await self.hub.push("problem", problem_id=problem_id, status=status,
                                answer=solved["display"])
        except asyncio.CancelledError:
            raise
        finally:
            current = asyncio.current_task()
            if self.answer_tasks.get(problem_id) is current:
                self.answer_tasks.pop(problem_id, None)


class _LessonFinished(Exception):
    pass


class AccountRuntime:
    def __init__(self, account_id: str, session: aiohttp.ClientSession,
                 client: core.YuketangClient, watcher: Watcher, account_hub: AccountHub):
        self.account_id = account_id
        self.session = session
        self.client = client
        self.watcher = watcher
        self.hub = account_hub
        self.closing = False
        self.op_lock = asyncio.Lock()

    def public_state(self, session_saved: bool) -> dict:
        state = dict(self.hub.state)
        state.update({
            "account_id": self.account_id,
            "user_id": self.client.user_id,
            "user_name": self.client.user_name,
            "logged_in": bool(self.client.user_id),
            "session_saved": session_saved,
            "watching": bool(self.watcher.running),
        })
        return state

    async def close(self):
        async with self.op_lock:
            if self.closing:
                return
            self.closing = True
            await self.watcher.stop()
            self.client.clear_session()
            if not self.session.closed:
                await self.session.close()


class AccountManager:
    def __init__(self, config: dict, shared_solver: core.LLMSolver, panel: Hub):
        self.cfg = config
        self.solver = shared_solver
        self.panel = panel
        self.notifier = emailer.EmailNotifier(
            config.get("email", {}), getattr(panel, "log", None),
        )
        self.answer_pool = SharedAnswerPool()
        self.accounts = {}
        self.records = load_account_records()
        self.lock = asyncio.Lock()
        self.closing = False

    def _new_runtime(self, account_id: str, session: aiohttp.ClientSession,
                     account_client: core.YuketangClient) -> AccountRuntime:
        account_hub = AccountHub(
            self.panel, account_id, account_client.user_name or account_client.user_id,
        )
        account_hub.state["dry_run"] = self.cfg["bot"].get("dry_run", True)
        account_watcher = Watcher(
            self.cfg, account_client, self.solver, account_hub,
            self.answer_pool, self.notifier,
        )
        return AccountRuntime(account_id, session, account_client, account_watcher, account_hub)

    async def restore_all(self):
        tasks = [self._restore_record(account_id, record)
                 for account_id, record in list(self.records.items())]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            async with self.lock:
                save_account_records(self.records)
        _refresh_legacy_aliases()
        await self.panel.sync_state()

    async def _restore_record(self, account_id: str, record: dict):
        if self.closing:
            return
        session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())
        account_cfg = {**self.cfg, "server": record["server"]}
        account_client = core.YuketangClient(account_cfg, session)
        try:
            if not await account_client.restore_session(record):
                await self._notify_saved_session_issue(
                    account_id, record, "雨课堂账号授权已失效",
                    "保存的登录态已失效，请重新扫码登录。",
                )
                self.panel.log_account(
                    account_id, record.get("user_name") or record.get("user_id"),
                    "保存的登录态已失效，请重新扫码", "warn",
                )
                return
            actual_id = account_id_for(account_client.server_key, account_client.user_id)
            runtime = self._new_runtime(actual_id, session, account_client)
            async with self.lock:
                if self.closing or actual_id in self.accounts:
                    return
                if actual_id != account_id:
                    self.records.pop(account_id, None)
                    self.records[actual_id] = client_session_record(account_client, actual_id)
                self.accounts[actual_id] = runtime
            self.panel.log_account(
                actual_id, account_client.user_name or account_client.user_id,
                "已恢复账号会话",
            )
            if self.cfg["bot"].get("auto_start_watching", True):
                runtime.watcher.start()
            else:
                runtime.watcher.set_state("idle", "账号会话已恢复")
            session = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._notify_saved_session_issue(
                account_id, record, "雨课堂账号会话恢复失败",
                f"服务启动时无法恢复账号会话: {str(exc)[:1000]}",
            )
            self.panel.log_account(
                account_id, record.get("user_name") or record.get("user_id"),
                f"账号会话恢复失败: {exc}", "warn",
            )
        finally:
            if session is not None and not session.closed:
                await session.close()

    async def _notify_saved_session_issue(self, account_id, record, subject, detail):
        account_name = record.get("user_name") or record.get("user_id") or account_id
        body = (
            "雨课堂答题面板检测到账号会话问题。\n\n"
            f"账号: {account_name}\n"
            f"服务: {record.get('server') or '-'}\n"
            f"详情: {str(detail)[:1000]}\n"
            f"时间: {time.strftime('%Y-%m-%d %H:%M:%S %z')}\n"
        )
        await self.notifier.notify(f"{account_id}:authorization", subject, body)

    async def add_verified(self, source: core.YuketangClient):
        if self.closing:
            raise RuntimeError("服务正在关闭")
        account_id = account_id_for(source.server_key, source.user_id)
        async with self.lock:
            existing = self.accounts.get(account_id)
        if existing is not None:
            self.notifier.clear_prefix(f"{account_id}:")
            return existing, False

        session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())
        account_cfg = {**self.cfg, "server": source.server_key}
        account_client = core.YuketangClient(account_cfg, session)
        try:
            account_client.adopt_login(source)
            runtime = self._new_runtime(account_id, session, account_client)
            record = client_session_record(account_client, account_id)
            async with self.lock:
                existing = self.accounts.get(account_id)
                if existing is None:
                    candidate = dict(self.records)
                    candidate[account_id] = record
                    save_account_records(candidate)
                    self.records = candidate
                    self.accounts[account_id] = runtime
            if existing is not None:
                self.notifier.clear_prefix(f"{account_id}:")
                return existing, False
            session = None
            _refresh_legacy_aliases()
            self.notifier.clear_prefix(f"{account_id}:")
            if self.cfg["bot"].get("auto_start_watching", True):
                runtime.watcher.start()
            else:
                runtime.watcher.set_state("idle", "账号已登录")
            await self.panel.sync_state()
            return runtime, True
        finally:
            if session is not None and not session.closed:
                await session.close()

    def get(self, account_id: str) -> AccountRuntime:
        runtime = self.accounts.get(str(account_id))
        if runtime is None or runtime.closing:
            raise KeyError("账号不存在或登录态无效")
        return runtime

    async def remove(self, account_id: str) -> bool:
        account_id = str(account_id)
        async with self.lock:
            if account_id not in self.records and account_id not in self.accounts:
                return False
            candidate = dict(self.records)
            candidate.pop(account_id, None)
            save_account_records(candidate)
            self.records = candidate
            runtime = self.accounts.pop(account_id, None)
            if not self.accounts:
                await self.answer_pool.clear()
        _refresh_legacy_aliases()
        self.notifier.clear_prefix(f"{account_id}:")
        if runtime is not None:
            await runtime.close()
        await self.panel.sync_state()
        return True

    async def clear(self):
        async with self.lock:
            account_ids = set(self.records) | set(self.accounts)
            save_account_records({})
            self.records = {}
            runtimes = list(self.accounts.values())
            self.accounts = {}
            await self.answer_pool.clear()
        _refresh_legacy_aliases()
        for account_id in account_ids:
            self.notifier.clear_prefix(f"{account_id}:")
        if runtimes:
            await asyncio.gather(*(runtime.close() for runtime in runtimes),
                                 return_exceptions=True)
        await self.panel.sync_state()

    async def scan_all(self, qr_content: str):
        async with self.lock:
            runtimes = list(self.accounts.values())
        semaphore = asyncio.Semaphore(8)

        async def scan_one(runtime):
            async with semaphore, runtime.op_lock:
                if runtime.closing:
                    return {"ok": False, "message": "账号正在关闭"}
                try:
                    rooms = await runtime.client.get_on_lesson()
                    enrolled_lessons = {
                        str(room.get("lessonId") or room.get("lesson_id"))
                        for room in rooms if isinstance(room, dict)
                    }
                    lesson_id = await runtime.client.scan_qr(qr_content)
                    if lesson_id is None or not str(lesson_id).strip():
                        raise RuntimeError("二维码未返回课堂标识")
                    lesson_id = str(lesson_id)
                    if lesson_id not in enrolled_lessons:
                        runtime.hub.log(
                            f"跳过扫码签到: 账号未加入 lessonId={lesson_id}", "warn",
                        )
                        return {
                            "ok": False, "status": "not_enrolled",
                            "lesson_id": lesson_id, "message": "账号未加入该课程",
                        }
                    result = await runtime.client.checkin(lesson_id, join_if_not_in=False)
                except core.QRCodeScanError as exc:
                    runtime.hub.log(f"扫码签到失败: {exc}", "error")
                    return {"ok": False, "code": exc.code, "message": str(exc)}
                except core.AuthenticationExpired as exc:
                    runtime.watcher._notify_issue(
                        "authorization", "雨课堂账号授权已失效", str(exc),
                    )
                    runtime.hub.log(f"扫码签到失败: {exc}", "error")
                    return {"ok": False, "message": "账号授权已失效"}
                except Exception as exc:
                    runtime.hub.log(f"扫码签到失败: {exc}", "error")
                    return {"ok": False, "message": str(exc)[:300]}

                if not result.get("ok"):
                    message = str(result.get("message") or "签到失败")[:300]
                    runtime.hub.log(f"扫码签到失败: {message}", "error")
                    return {
                        "ok": False, "lesson_id": lesson_id,
                        "code": result.get("code"), "message": message,
                    }
                watcher = runtime.watcher
                credentials_applied = (
                    not watcher.running or not watcher.lesson_id or watcher.lesson_id == lesson_id
                )
                if credentials_applied:
                    runtime.client.bearer = result["bearer"]
                    runtime.client.lesson_token = result["lesson_token"]
                if watcher.running and watcher.lesson_id == lesson_id:
                    watcher._manual_join_ready_for = lesson_id
                    watcher._manual_join_event.set()
                runtime.hub.log(f"扫码签到成功: lessonId={lesson_id}")
                return {
                    "ok": True, "lesson_id": lesson_id,
                    "credentials_applied": credentials_applied,
                }

        values = await asyncio.gather(*(scan_one(runtime) for runtime in runtimes))
        results = []
        for runtime, result in zip(runtimes, values):
            results.append({
                "account_id": runtime.account_id,
                "account_name": runtime.client.user_name or runtime.client.user_id,
                **result,
            })
        return {
            "total": len(results),
            "success": sum(bool(item.get("ok")) for item in results),
            "results": results,
        }

    async def start_all(self):
        results = {}
        for account_id, runtime in list(self.accounts.items()):
            results[account_id] = runtime.watcher.start()
        await self.panel.sync_state()
        return results

    async def stop_all(self):
        runtimes = list(self.accounts.values())
        if runtimes:
            await asyncio.gather(*(runtime.watcher.stop() for runtime in runtimes),
                                 return_exceptions=True)
        await self.answer_pool.clear()
        await self.panel.sync_state()

    def set_solver(self, value: core.LLMSolver):
        self.solver = value
        # Existing entries stay pinned so late accounts submit the same answer.
        for runtime in self.accounts.values():
            runtime.watcher.solver = value

    def set_email_config(self, settings):
        self.notifier.configure(settings)

    def public_accounts(self) -> list[dict]:
        values = [runtime.public_state(account_id in self.records)
                  for account_id, runtime in self.accounts.items()]
        active_ids = set(self.accounts)
        for account_id, record in self.records.items():
            if account_id in active_ids:
                continue
            values.append({
                "account_id": account_id,
                "user_id": record.get("user_id", ""),
                "user_name": record.get("user_name", ""),
                "logged_in": False,
                "session_saved": True,
                "watching": False,
                "phase": "expired",
                "detail": "登录态无效，请重新扫码",
                "lesson_id": "",
                "course_name": "",
                "answered": 0,
                "problems": 0,
                "dry_run": self.cfg["bot"].get("dry_run", True),
            })
        return values

    async def shutdown(self):
        self.closing = True
        runtimes = list(self.accounts.values())
        if runtimes:
            await asyncio.gather(*(runtime.close() for runtime in runtimes),
                                 return_exceptions=True)
        self.accounts.clear()
        await self.notifier.close()
        await self.answer_pool.clear()


# ---------------------------------------------------------------------------
# Web 应用
# ---------------------------------------------------------------------------

hub = Hub()
cfg = load_config()
http_session = None
llm_session = None
client = None
solver = None
watcher = None
account_manager = None
qr_generation = 0


class QRAttempt:
    def __init__(self, attempt_id: int, session: aiohttp.ClientSession):
        self.attempt_id = attempt_id
        self.session = session
        self.client = core.YuketangClient(cfg, session)
        self.token = ""
        self.tasks = set()
        self.commit_task = None


qr_attempt = None
config_revision = 0
AUTH_COOKIE_NAME = "ykt_admin_session"
SCANNER_COOKIE_NAME = "ykt_scanner_session"


def _admin_password_matches(settings, password):
    if settings.get("password_hash"):
        candidate = _password_digest(
            password, settings["password_salt"], settings["password_iterations"],
        )
        return hmac.compare_digest(candidate, settings["password_hash"])
    return hmac.compare_digest(
        password.encode("utf-8"), settings["password"].encode("utf-8")
    )


def _scanner_password_matches(settings, password):
    if not settings.get("scanner_enabled"):
        return False
    candidate = _password_digest(
        password,
        settings["scanner_password_salt"],
        settings["scanner_password_iterations"],
    )
    return hmac.compare_digest(candidate, settings["scanner_password_hash"])


def _admin_session_token(settings, issued_at=None):
    issued_at = int(time.time()) if issued_at is None else int(issued_at)
    message = f"ykt-web-admin-session-v1\0{settings['username']}\0{issued_at}".encode("utf-8")
    credential_secret = settings.get("password_hash") or settings["password"].encode("utf-8")
    key = hmac.new(credential_secret, settings["session_secret"], hashlib.sha256).digest()
    signature = hmac.new(
        key, message, hashlib.sha256,
    ).hexdigest()
    return f"{issued_at}.{signature}"


def _scanner_session_token(settings, issued_at=None):
    issued_at = int(time.time()) if issued_at is None else int(issued_at)
    message = f"ykt-web-scanner-session-v1\0{issued_at}".encode("utf-8")
    key = hmac.new(
        settings["scanner_password_hash"],
        settings["scanner_session_secret"],
        hashlib.sha256,
    ).digest()
    signature = hmac.new(key, message, hashlib.sha256).hexdigest()
    return f"{issued_at}.{signature}"


def _secure_cookie_required(request, settings):
    return (settings["secure_cookie"] or request.secure
            or (_request_uses_trusted_proxy(request)
                and request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip()
                == "https"))


def _set_admin_session_cookie(response, request, settings):
    response.set_cookie(
        AUTH_COOKIE_NAME, _admin_session_token(settings), max_age=30 * 24 * 60 * 60,
        httponly=True, secure=_secure_cookie_required(request, settings),
        samesite="Strict", path="/",
    )


def _set_scanner_session_cookie(response, request, settings):
    response.set_cookie(
        SCANNER_COOKIE_NAME, _scanner_session_token(settings), max_age=24 * 60 * 60,
        httponly=True, secure=_secure_cookie_required(request, settings),
        samesite="Strict", path="/",
    )


async def _close_admin_sessions(app, *, rotate_secret=True):
    if rotate_secret:
        app["runtime_config"]["session_secret"] = secrets.token_bytes(32)
    clients = getattr(hub, "clients", set())
    sockets = list(clients)
    for socket in sockets:
        clients.discard(socket)
    if sockets:
        await asyncio.gather(
            *(socket.close(code=4001, message=b"admin session changed") for socket in sockets),
            return_exceptions=True,
        )


def _request_cookie_is_valid(request, cookie_name, token_factory, max_age):
    supplied = request.cookies.get(cookie_name, "")
    try:
        issued_text, _ = supplied.split(".", 1)
        issued_at = int(issued_text)
    except (AttributeError, TypeError, ValueError):
        return False
    age = int(time.time()) - issued_at
    if age < -300 or age > max_age:
        return False
    return hmac.compare_digest(
        supplied, token_factory(request.app["runtime_config"], issued_at),
    )


def _request_is_authenticated(request):
    settings = request.app["runtime_config"]
    if not settings["auth_enabled"]:
        return True
    return _request_cookie_is_valid(
        request, AUTH_COOKIE_NAME, _admin_session_token, 30 * 24 * 60 * 60,
    )


def _request_is_scanner_authenticated(request):
    settings = request.app["runtime_config"]
    return bool(settings.get("scanner_enabled")) and _request_cookie_is_valid(
        request, SCANNER_COOKIE_NAME, _scanner_session_token, 24 * 60 * 60,
    )


def _admin_session_age(request):
    supplied = request.cookies.get(AUTH_COOKIE_NAME, "")
    try:
        issued_at = int(supplied.split(".", 1)[0])
    except (AttributeError, TypeError, ValueError):
        return None
    return max(0, int(time.time()) - issued_at)


@web.middleware
async def admin_auth_middleware(request, handler):
    public_requests = {
        ("GET", "/"), ("GET", "/scan"), ("GET", "/api/health"),
        ("GET", "/api/auth/status"), ("POST", "/api/auth/login"),
        ("GET", "/api/scanner/status"), ("POST", "/api/scanner/login"),
    }
    scanner_requests = {
        ("POST", "/api/accounts/scan-all"), ("POST", "/api/scanner/logout"),
    }
    request_key = (request.method, request.path)
    settings = request.app["runtime_config"]
    if request_key in public_requests or (
            request.method == "GET" and request.path.startswith("/vendor/")):
        return await handler(request)
    if (request_key in scanner_requests
            and (_request_is_authenticated(request)
                 or _request_is_scanner_authenticated(request))):
        return await handler(request)
    if not settings["auth_enabled"] or _request_is_authenticated(request):
        return await handler(request)
    return web.json_response(
        {"ok": False, "message": "管理员登录已失效，请重新登录"},
        status=401,
        headers={"Cache-Control": "no-store"},
    )


@web.middleware
async def csrf_origin_middleware(request, handler):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin_error = _local_origin_request_error(request)
        if origin_error is not None:
            return origin_error
    return await handler(request)


@web.middleware
async def security_headers_middleware(request, handler):
    response = await handler(request)
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
        "media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'",
    )
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
    return response


def _refresh_legacy_aliases():
    global http_session, client, watcher
    runtime = None
    if account_manager is not None and account_manager.accounts:
        runtime = next(iter(account_manager.accounts.values()))
    http_session = runtime.session if runtime else None
    client = runtime.client if runtime else None
    watcher = runtime.watcher if runtime else None


def public_state() -> dict:
    if account_manager is not None:
        account_states = account_manager.public_accounts()
        active = [item for item in account_states if item.get("logged_in")]
        primary = active[0] if active else (account_states[0] if account_states else {})
        state = {
            "phase": primary.get("phase", "idle"),
            "user_id": primary.get("user_id", ""),
            "user_name": primary.get("user_name", ""),
            "lesson_id": primary.get("lesson_id", ""),
            "course_name": primary.get("course_name", ""),
            "answered": sum(int(item.get("answered") or 0) for item in active),
            "problems": sum(int(item.get("problems") or 0) for item in active),
            "dry_run": cfg["bot"].get("dry_run", True),
            "detail": primary.get("detail", "未登录"),
            "logged_in": bool(active),
            "watching": any(item.get("watching") for item in active),
            "session_saved": any(item.get("session_saved") for item in account_states),
            "accounts": account_states,
            "summary": {
                "total": len(account_states),
                "logged_in": len(active),
                "watching": sum(bool(item.get("watching")) for item in active),
                "in_class": sum(item.get("phase") == "in_class" for item in active),
            },
        }
        state["auto_start_watching"] = cfg["bot"].get("auto_start_watching", True)
        state["auto_answer"] = not cfg["bot"].get("dry_run", True)
        state["manual_checkin"] = True
        state["llm"] = public_llm_config()
        return state
    state = dict(hub.state)
    state["logged_in"] = bool(client and client.user_id)
    state["user_id"] = client.user_id if client else ""
    state["user_name"] = client.user_name if client else ""
    state["watching"] = bool(watcher and watcher.running)
    state["session_saved"] = bool(hub.state.get("session_saved") and SESSION_PATH.exists())
    state["auto_start_watching"] = cfg["bot"].get("auto_start_watching", True)
    state["auto_answer"] = not cfg["bot"].get("dry_run", True)
    state["manual_checkin"] = True
    state["llm"] = public_llm_config()
    return state


def _qr_attempt_matches(attempt: QRAttempt, attempt_id: int, token: str) -> bool:
    return (qr_attempt is attempt and attempt.attempt_id == attempt_id == qr_generation
            and attempt.token == token)


def _qr_superseded_response():
    return web.json_response({"status": "superseded", "message": "二维码已更新"}, status=409)


async def _dispose_qr_attempt(attempt: QRAttempt, exclude_task=None):
    excluded = exclude_task or asyncio.current_task()
    tasks = [task for task in attempt.tasks if task is not excluded and not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    attempt.tasks.clear()
    if not attempt.session.closed:
        await attempt.session.close()


async def _retire_qr_attempt(attempt: QRAttempt):
    global qr_attempt
    if qr_attempt is attempt:
        qr_attempt = None
    await _dispose_qr_attempt(attempt)


async def _supersede_qr_attempt() -> int:
    global qr_generation, qr_attempt
    qr_generation += 1
    generation = qr_generation
    previous, qr_attempt = qr_attempt, None
    if previous is not None:
        await _dispose_qr_attempt(previous)
    return generation


async def index(request):
    return web.FileResponse(ROOT / "static" / "index.html", headers={"Cache-Control": "no-store"})


async def scanner_page(request):
    return web.FileResponse(ROOT / "static" / "scan.html", headers={"Cache-Control": "no-store"})


async def api_auth_status(request):
    settings = request.app["runtime_config"]
    return web.json_response({
        "required": settings["auth_enabled"],
        "authenticated": _request_is_authenticated(request),
    }, headers={"Cache-Control": "no-store"})


async def api_health(request):
    return web.json_response({"ok": True}, headers={"Cache-Control": "no-store"})


async def api_auth_login(request):
    request_error = _local_json_request_error(request)
    if request_error is not None:
        return request_error
    body = await request.json()
    if not isinstance(body, dict) or set(body) != {"username", "password"}:
        raise ValueError("管理员登录需要 username 和 password")
    username = body.get("username")
    password = body.get("password")
    if (not isinstance(username, str) or not isinstance(password, str)
            or len(username) > 128 or len(password) > 256):
        raise ValueError("管理员账号或密码无效")

    settings = request.app["runtime_config"]
    if not settings["auth_enabled"]:
        return web.json_response({"ok": True, "required": False, "authenticated": True},
                                 headers={"Cache-Control": "no-store"})

    remote = _request_client_id(request)
    now = time.monotonic()
    async with request.app["auth_lock"]:
        failures = request.app["auth_failures"]
        if len(failures) >= 1024:
            stale = [key for key, value in failures.items()
                     if now - value.get("last_seen", 0.0) > 600]
            for key in stale:
                failures.pop(key, None)
            while len(failures) >= 1024:
                failures.pop(next(iter(failures)))
        state = failures.get(remote, {
            "count": 0, "blocked_until": 0.0, "last_seen": now,
        })
        state["last_seen"] = now
        if state["blocked_until"] > now:
            return web.json_response(
                {"ok": False, "message": "登录尝试过于频繁，请稍后再试"},
                status=429, headers={"Cache-Control": "no-store"},
            )
        valid_user = hmac.compare_digest(
            username.encode("utf-8"), settings["username"].encode("utf-8")
        )
        valid_password = await asyncio.to_thread(_admin_password_matches, settings, password)
        if not (valid_user and valid_password):
            state["count"] += 1
            if state["count"] >= 5:
                state = {"count": 0, "blocked_until": now + 60, "last_seen": now}
            failures[remote] = state
            return web.json_response(
                {"ok": False, "message": "管理员账号或密码错误"},
                status=401, headers={"Cache-Control": "no-store"},
            )
        failures.pop(remote, None)
        response = web.json_response(
            {"ok": True, "authenticated": True},
            headers={"Cache-Control": "no-store"},
        )
        _set_admin_session_cookie(response, request, settings)
        return response


async def api_auth_logout(request):
    request_error = _local_json_request_error(request)
    if request_error is not None:
        return request_error
    await request.json()
    async with request.app["auth_lock"]:
        if not _request_is_authenticated(request):
            return web.json_response(
                {"ok": False, "message": "管理员登录已失效，请重新登录"}, status=401,
            )
        await _close_admin_sessions(request.app)
    response = web.json_response({"ok": True}, headers={"Cache-Control": "no-store"})
    response.del_cookie(AUTH_COOKIE_NAME, path="/")
    return response


async def api_scanner_status(request):
    settings = request.app["runtime_config"]
    admin_authenticated = _request_is_authenticated(request)
    return web.json_response({
        "configured": bool(settings.get("scanner_enabled")),
        "authenticated": (admin_authenticated
                          or _request_is_scanner_authenticated(request)),
        "admin": admin_authenticated,
    }, headers={"Cache-Control": "no-store"})


async def api_scanner_login(request):
    request_error = _local_json_request_error(request)
    if request_error is not None:
        return request_error
    body = await request.json()
    if not isinstance(body, dict) or set(body) != {"password"}:
        raise ValueError("扫码页登录需要 password")
    password = body.get("password")
    if not isinstance(password, str) or len(password) > 128:
        raise ValueError("扫码页密码无效")
    settings = request.app["runtime_config"]
    if not settings.get("scanner_enabled"):
        return web.json_response(
            {"ok": False, "message": "管理员尚未配置扫码页密码"}, status=409,
        )

    remote = _request_client_id(request)
    now = time.monotonic()
    async with request.app["scanner_auth_lock"]:
        failures = request.app["scanner_auth_failures"]
        if len(failures) >= 1024:
            stale = [key for key, value in failures.items()
                     if now - value.get("last_seen", 0.0) > 600]
            for key in stale:
                failures.pop(key, None)
            while len(failures) >= 1024:
                failures.pop(next(iter(failures)))
        state = failures.get(remote, {
            "count": 0, "blocked_until": 0.0, "last_seen": now,
        })
        state["last_seen"] = now
        if state["blocked_until"] > now:
            return web.json_response(
                {"ok": False, "message": "登录尝试过于频繁，请稍后再试"},
                status=429, headers={"Cache-Control": "no-store"},
            )
        valid = await asyncio.to_thread(_scanner_password_matches, settings, password)
        if not valid:
            state["count"] += 1
            if state["count"] >= 5:
                state = {"count": 0, "blocked_until": now + 60, "last_seen": now}
            failures[remote] = state
            return web.json_response(
                {"ok": False, "message": "扫码页密码错误"}, status=401,
                headers={"Cache-Control": "no-store"},
            )
        failures.pop(remote, None)
        response = web.json_response(
            {"ok": True, "authenticated": True}, headers={"Cache-Control": "no-store"},
        )
        _set_scanner_session_cookie(response, request, settings)
        return response


async def api_scanner_logout(request):
    request_error = _local_json_request_error(request)
    if request_error is not None:
        return request_error
    await request.json()
    async with request.app["scanner_auth_lock"]:
        if (not _request_is_authenticated(request)
                and not _request_is_scanner_authenticated(request)):
            return web.json_response(
                {"ok": False, "message": "扫码页登录已失效，请重新登录"}, status=401,
            )
        request.app["runtime_config"]["scanner_session_secret"] = secrets.token_bytes(32)
    response = web.json_response({"ok": True}, headers={"Cache-Control": "no-store"})
    response.del_cookie(SCANNER_COOKIE_NAME, path="/")
    return response


async def api_scanner_credentials(request):
    request_error = _local_json_request_error(request)
    if request_error is not None:
        return request_error
    body = await request.json()
    if not isinstance(body, dict) or set(body) != {"new_password"}:
        raise ValueError("扫码页密码请求字段不完整")
    password = _validate_scanner_password(body.get("new_password"))

    async with request.app["auth_lock"]:
        if not _request_is_authenticated(request):
            return web.json_response(
                {"ok": False, "message": "管理员登录已失效，请重新登录"}, status=401,
            )
        settings = request.app["runtime_config"]
        if settings["auth_enabled"]:
            age = _admin_session_age(request)
            if age is None or age > 10 * 60:
                return web.json_response(
                    {"ok": False, "message": "请重新登录后再设置扫码页密码"}, status=403,
                )
        async with request.app["scanner_auth_lock"]:
            async with request.app["config_lock"]:
                salt = secrets.token_bytes(16)
                password_hash = await asyncio.to_thread(
                    _password_digest, password, salt, SCANNER_PASSWORD_ITERATIONS,
                )
                scanner_record = {
                    "password_salt": salt.hex(),
                    "password_hash": password_hash.hex(),
                    "password_iterations": SCANNER_PASSWORD_ITERATIONS,
                }
                next_cfg = {**cfg, "scanner": scanner_record}
                save_config(next_cfg)
                cfg.clear()
                cfg.update(next_cfg)
                settings = request.app["runtime_config"]
                settings.update({
                    "scanner_enabled": True,
                    "scanner_password_salt": salt,
                    "scanner_password_hash": password_hash,
                    "scanner_password_iterations": SCANNER_PASSWORD_ITERATIONS,
                    "scanner_session_secret": secrets.token_bytes(32),
                })

    hub.log("扫码页访问密码已更新", "warn")
    return web.json_response(
        {"ok": True, "scanner": public_scanner_config(request)},
        headers={"Cache-Control": "no-store"},
    )


async def api_admin_credentials(request):
    request_error = _local_json_request_error(request)
    if request_error is not None:
        return request_error
    body = await request.json()
    if not isinstance(body, dict) or set(body) != {"new_password"}:
        raise ValueError("管理员密码请求字段不完整")
    try:
        new_password = _validate_admin_password(body.get("new_password"))
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    async with request.app["auth_lock"]:
        settings = request.app["runtime_config"]
        if not _request_is_authenticated(request):
            return web.json_response(
                {"ok": False, "message": "管理员登录已失效，请重新登录"}, status=401,
            )
        if settings["auth_enabled"]:
            age = _admin_session_age(request)
            if age is None or age > 10 * 60:
                return web.json_response(
                    {"ok": False, "message": "请重新登录后再修改管理员密码"},
                    status=403, headers={"Cache-Control": "no-store"},
                )
        async with request.app["config_lock"]:
            username = settings["username"]
            salt = secrets.token_bytes(16)
            password_hash = await asyncio.to_thread(
                _password_digest, new_password, salt, ADMIN_PASSWORD_ITERATIONS,
            )
            admin_record = {
                "username": username,
                "password_salt": salt.hex(),
                "password_hash": password_hash.hex(),
                "password_iterations": ADMIN_PASSWORD_ITERATIONS,
            }
            next_cfg = {**cfg, "admin": admin_record}
            save_config(next_cfg)
            cfg.clear()
            cfg.update(next_cfg)
            settings.update({
                "auth_enabled": True,
                "username": username,
                "password": "",
                "password_salt": salt,
                "password_hash": password_hash,
                "password_iterations": ADMIN_PASSWORD_ITERATIONS,
                "session_secret": secrets.token_bytes(32),
            })

        await _close_admin_sessions(request.app, rotate_secret=False)
        hub.log("管理员账号凭据已更新", "warn")
        response = web.json_response(
            {"ok": True, "admin": {
                "username": username,
                "password_required": True,
                "reset_mode": bool(settings.get("admin_reset", False)),
            }},
            headers={"Cache-Control": "no-store"},
        )
        _set_admin_session_cookie(response, request, settings)
        return response


async def _cancel_restore(app):
    task = app.get("restore_task")
    if not task or task.done() or task is asyncio.current_task():
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@web.middleware
async def api_error_middleware(request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        return web.json_response({"ok": False, "message": f"请求参数无效: {exc}"}, status=400)
    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
        hub.log(f"上游请求失败: {exc}", "error")
        return web.json_response({"ok": False, "message": str(exc)}, status=502)
    except Exception as exc:
        hub.log(f"本地服务异常: {exc}", "error")
        return web.json_response({"ok": False, "message": "本地服务发生异常"}, status=500)


async def ws_handler(request):
    origin_error = _local_origin_request_error(request)
    if origin_error is not None:
        return origin_error
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)
    if (request.app["runtime_config"]["auth_enabled"]
            and not _request_is_authenticated(request)):
        await ws.close(code=4001, message=b"admin session expired")
        return ws
    hub.clients.add(ws)
    for m in list(hub.history)[-120:]:
        try:
            if json.loads(m).get("kind") == "log":
                await ws.send_str(m)
        except (TypeError, json.JSONDecodeError):
            continue
    if account_manager is not None:
        for runtime in account_manager.accounts.values():
            if runtime.hub.current_problem:
                await ws.send_str(json.dumps(runtime.hub.current_problem, ensure_ascii=False))
    elif hub.current_problem and hub.state.get("phase") in ("in_class", "reconnecting"):
        await ws.send_str(json.dumps(hub.current_problem, ensure_ascii=False))
    await ws.send_str(json.dumps({"kind": "state", "ts": time.time(), **public_state()},
                                 ensure_ascii=False))
    try:
        async for msg in ws:
            pass
    finally:
        hub.clients.discard(ws)
    return ws


async def api_state(request):
    return web.json_response(public_state())


async def api_login_qr_start(request):
    global qr_attempt
    if account_manager is None:
        await _cancel_restore(request.app)
    attempt_id = await _supersede_qr_attempt()
    if attempt_id != qr_generation:
        return web.json_response({"ok": False, "message": "二维码请求已被新的请求替代"}, status=409)
    session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())
    attempt = QRAttempt(attempt_id, session)
    qr_attempt = attempt
    task = asyncio.current_task()
    attempt.tasks.add(task)
    try:
        data = await attempt.client.qr_start()
        if qr_attempt is not attempt or attempt_id != qr_generation:
            return _qr_superseded_response()
        attempt.token = data["token"]
        data["attempt_id"] = attempt_id
        scan_name = "微信" if data.get("scan_app") == "wechat" else "雨课堂 App"
        hub.log(f"扫码登录: 二维码已生成, 请用{scan_name}扫码")
        return web.json_response(data)
    except asyncio.CancelledError:
        raise
    except Exception:
        if qr_attempt is attempt:
            qr_attempt = None
        await _dispose_qr_attempt(attempt)
        raise
    finally:
        attempt.tasks.discard(task)


async def api_login_qr_poll(request):
    body = await request.json()
    token = str(body.get("token") or "").strip()
    if "attempt_id" not in body or body["attempt_id"] is None or body["attempt_id"] == "":
        raise ValueError("缺少二维码 attempt_id")
    try:
        raw_attempt_id = body["attempt_id"]
        if isinstance(raw_attempt_id, bool):
            raise ValueError
        attempt_id = int(raw_attempt_id)
        if attempt_id < 1 or (isinstance(raw_attempt_id, float) and not raw_attempt_id.is_integer()):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("二维码 attempt_id 无效")
    if not token:
        raise ValueError("缺少二维码 token")
    attempt = qr_attempt
    if attempt is None or not _qr_attempt_matches(attempt, attempt_id, token):
        return _qr_superseded_response()
    task = asyncio.current_task()
    attempt.tasks.add(task)
    try:
        try:
            result = await attempt.client.qr_poll(token)
        except RuntimeError as exc:
            if not _qr_attempt_matches(attempt, attempt_id, token):
                return _qr_superseded_response()
            await hub.push("login", status="failed", message=str(exc), attempt_id=attempt_id)
            if not _qr_attempt_matches(attempt, attempt_id, token):
                return _qr_superseded_response()
            await _retire_qr_attempt(attempt)
            return web.json_response({"status": "failed", "message": str(exc)}, status=502)
        if not _qr_attempt_matches(attempt, attempt_id, token):
            return _qr_superseded_response()
        if isinstance(result, dict) and result.get("status") == "refresh":
            code = result.get("code")
            message = "二维码等待超时，正在自动刷新"
            hub.log(f"扫码登录: {message}" + (f" (code={code})" if code is not None else ""))
            await _retire_qr_attempt(attempt)
            return web.json_response({"status": "refresh", "code": code, "message": message})
        if result == "pending":
            return web.json_response({"status": "pending"})
        if attempt.commit_task is not None:
            return _qr_superseded_response()
        return await _finish_login(attempt_id, token, attempt)
    except asyncio.CancelledError:
        if attempt.commit_task is task and not attempt.session.closed:
            await asyncio.shield(_dispose_qr_attempt(attempt, exclude_task=task))
        raise
    except Exception:
        if attempt.commit_task is task and not attempt.session.closed:
            await _dispose_qr_attempt(attempt, exclude_task=task)
        raise
    finally:
        attempt.tasks.discard(task)


async def api_login_cookie(request):
    await _supersede_qr_attempt()
    body = await request.json()
    if account_manager is None:
        await _cancel_restore(request.app)
        ok = await client.restore_cookie(body.get("cookie", ""))
        if not ok:
            return web.json_response({"status": "failed", "message": "cookie 无效"})
        return await _finish_login()
    session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())
    source = core.YuketangClient(cfg, session)
    try:
        if not await source.restore_cookie(body.get("cookie", "")):
            return web.json_response({"status": "failed", "message": "cookie 无效"})
        runtime, created = await account_manager.add_verified(source)
    finally:
        await session.close()
    await hub.push("login", status="ok", account_id=runtime.account_id,
                   user_id=runtime.client.user_id, user_name=runtime.client.user_name,
                   session_saved=True, created=created)
    return web.json_response({"status": "ok", "account_id": runtime.account_id,
                              "user_id": runtime.client.user_id,
                              "user_name": runtime.client.user_name,
                              "session_saved": True, "created": created})


async def _finish_login(attempt_id=None, token=None, attempt=None):
    global qr_attempt
    guarded = attempt is not None
    if guarded and not _qr_attempt_matches(attempt, attempt_id, token):
        return _qr_superseded_response()
    if guarded:
        if attempt.commit_task is not None or not _qr_attempt_matches(attempt, attempt_id, token):
            return _qr_superseded_response()
        # Linearization point: no await until the verified login is fully installed.
        attempt.commit_task = asyncio.current_task()
        qr_attempt = None
    if account_manager is not None:
        if not guarded:
            raise RuntimeError("多账号登录需要隔离的登录会话")
        runtime, created = await account_manager.add_verified(attempt.client)
        if guarded:
            await _dispose_qr_attempt(attempt)
        label = runtime.client.user_name or runtime.client.user_id
        hub.log(("登录成功并添加账号: " if created else "账号已存在: ") + label)
        await hub.push("login", status="ok", account_id=runtime.account_id,
                       user_id=runtime.client.user_id, user_name=runtime.client.user_name,
                       session_saved=True, attempt_id=attempt_id, created=created)
        return web.json_response({"status": "ok", "account_id": runtime.account_id,
                                  "user_id": runtime.client.user_id,
                                  "user_name": runtime.client.user_name,
                                  "session_saved": True, "created": created})
    if guarded:
        client.adopt_login(attempt.client)
    hub.state["user_id"] = client.user_id
    hub.state["user_name"] = client.user_name
    try:
        saved = save_session(client)
    except OSError as exc:
        saved = False
        hub.log(f"登录成功，但账号会话保存失败: {exc}", "warn")
    hub.state["session_saved"] = saved
    label = client.user_name or client.user_id
    hub.log(f"登录成功: {label}" + ("，会话已保存" if saved else ""))
    if cfg["bot"].get("auto_start_watching", True):
        watcher.start()
    if guarded:
        await _dispose_qr_attempt(attempt)
    await hub.push("login", status="ok", user_id=client.user_id,
                   user_name=client.user_name, session_saved=saved, attempt_id=attempt_id)
    return web.json_response({"status": "ok", "user_id": client.user_id,
                              "user_name": client.user_name, "session_saved": saved})


async def api_logout(request):
    if account_manager is not None:
        await _supersede_qr_attempt()
        await account_manager.clear()
        hub.log("已退出全部账号并删除本地保存的会话", "warn")
        await hub.push("login", status="logged_out", message="已退出全部账号")
        return web.json_response({"ok": True})
    await _cancel_restore(request.app)
    await _supersede_qr_attempt()
    await watcher.stop()
    # Invalidate a QR start that raced with watcher shutdown before clearing the account.
    await _supersede_qr_attempt()
    client.clear_session()
    try:
        SESSION_PATH.unlink(missing_ok=True)
    except OSError as exc:
        return web.json_response({"ok": False, "message": f"删除本地会话失败: {exc}"}, status=500)
    hub.state.update({"user_id": "", "user_name": "", "session_saved": False})
    hub.log("已退出账号并删除本地保存的会话", "warn")
    await hub.push("login", status="logged_out", message="已退出账号")
    return web.json_response({"ok": True})


async def api_watch_start(request):
    if account_manager is not None:
        if not account_manager.accounts:
            return web.json_response({"ok": False, "message": "请先添加账号"}, status=400)
        return web.json_response({"ok": True,
                                  "results": await account_manager.start_all()})
    if not client.user_id:
        return web.json_response({"ok": False, "message": "请先登录"}, status=400)
    started = watcher.start()
    return web.json_response({"ok": True, "started": started})


async def api_watch_stop(request):
    if account_manager is not None:
        await account_manager.stop_all()
        return web.json_response({"ok": True})
    await watcher.stop()
    return web.json_response({"ok": True})


async def api_watch_join(request):
    if account_manager is not None:
        active = list(account_manager.accounts.values())
        if len(active) != 1:
            return web.json_response({"ok": False, "message": "请指定账号"}, status=409)
        result = await active[0].watcher.recheck_manual_join()
        return web.json_response(result, status=200 if result.get("ok") else 409)
    result = await watcher.recheck_manual_join()
    return web.json_response(result, status=200 if result.get("ok") else 409)


def _request_account(request) -> AccountRuntime:
    if account_manager is None:
        raise KeyError("多账号管理器未启动")
    return account_manager.get(request.match_info["account_id"])


async def api_account_watch_start(request):
    runtime = _request_account(request)
    async with runtime.op_lock:
        started = runtime.watcher.start()
    return web.json_response({"ok": True, "started": started})


async def api_account_watch_stop(request):
    runtime = _request_account(request)
    async with runtime.op_lock:
        await runtime.watcher.stop()
    return web.json_response({"ok": True})


async def api_account_watch_join(request):
    runtime = _request_account(request)
    result = await runtime.watcher.recheck_manual_join()
    return web.json_response(result, status=200 if result.get("ok") else 409)


async def api_account_remove(request):
    request_error = _local_json_request_error(request)
    if request_error is not None:
        return request_error
    await request.json()
    account_id = request.match_info["account_id"]
    if not await account_manager.remove(account_id):
        return web.json_response({"ok": False, "message": "账号不存在"}, status=404)
    hub.log(f"已移除账号 {account_id}", "warn")
    await hub.push("login", status="removed", account_id=account_id)
    return web.json_response({"ok": True})


async def api_accounts_scan_all(request):
    request_error = _local_json_request_error(request)
    if request_error is not None:
        return request_error
    admin_request = _request_is_authenticated(request)
    if not admin_request and not request.headers.get("Origin"):
        return web.json_response(
            {"ok": False, "message": "扫码请求缺少同源标识"}, status=403,
        )
    body = await request.json()
    if not isinstance(body, dict) or set(body) != {"qr_content"}:
        raise ValueError("扫码签到请求需要 qr_content")
    qr_content = body.get("qr_content")
    if not isinstance(qr_content, str):
        raise ValueError("二维码内容必须是字符串")
    qr_content = core.validate_checkin_qr_url(qr_content)
    if account_manager is None or not account_manager.accounts:
        return web.json_response(
            {"ok": False, "message": "没有可签到的已登录账号"}, status=409,
        )
    scan_lock = request.app["scan_lock"]
    if scan_lock.locked():
        return web.json_response(
            {"ok": False, "message": "已有扫码签到任务正在执行"}, status=409,
        )
    now = time.monotonic()
    if now - request.app["last_scan_at"] < 2:
        return web.json_response(
            {"ok": False, "message": "扫码签到过于频繁，请稍后再试"}, status=429,
        )
    async with scan_lock:
        result = await account_manager.scan_all(qr_content)
        request.app["last_scan_at"] = time.monotonic()
    hub.log(f"扫码签到完成: {result['success']}/{result['total']} 个账号成功")
    if admin_request:
        return web.json_response({"ok": True, **result})

    failures = {}
    for item in result["results"]:
        if item.get("ok"):
            continue
        if item.get("status") == "not_enrolled":
            reason = "not_enrolled"
        elif str(item.get("code")) == "51203":
            reason = "expired"
        elif "授权" in str(item.get("message") or ""):
            reason = "authorization"
        else:
            reason = "failed"
        failures[reason] = failures.get(reason, 0) + 1
    return web.json_response({
        "ok": True,
        "total": result["total"],
        "success": result["success"],
        "failed": result["total"] - result["success"],
        "failures": failures,
    })


def _local_json_request_error(request):
    if getattr(request, "content_type", "") != "application/json":
        return web.json_response({"ok": False, "message": "请求必须使用 application/json"},
                                 status=415)
    return _local_origin_request_error(request)


def _request_uses_trusted_proxy(request):
    runtime = getattr(request, "app", {}).get("runtime_config", {})
    if not runtime.get("trust_proxy"):
        return False
    remote = str(getattr(request, "remote", "") or "")
    try:
        return ipaddress.ip_address(remote).is_loopback
    except ValueError:
        return remote == "localhost"


def _request_client_id(request):
    if _request_uses_trusted_proxy(request):
        forwarded = request.headers.get("X-Forwarded-For", "").rsplit(",", 1)[-1].strip()
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return str(getattr(request, "remote", None) or "unknown")


def _local_origin_request_error(request):
    origin = getattr(request, "headers", {}).get("Origin")
    if origin:
        scheme = request.scheme
        host = request.host
        if _request_uses_trusted_proxy(request):
            forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip()
            forwarded_host = request.headers.get("X-Forwarded-Host", "").split(",", 1)[0].strip()
            if forwarded_proto in ("http", "https"):
                scheme = forwarded_proto
            if (forwarded_host and len(forwarded_host) <= 253
                    and not any(char.isspace() or ord(char) < 32 for char in forwarded_host)
                    and "/" not in forwarded_host):
                host = forwarded_host
        expected_origin = f"{scheme}://{host}"
        if origin.rstrip("/") != expected_origin.rstrip("/"):
            return web.json_response({"ok": False, "message": "拒绝跨站请求"}, status=403)
    return None


async def api_config(request):
    global solver, config_revision
    request_error = _local_json_request_error(request)
    if request_error is not None:
        return request_error
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError("配置必须是 JSON 对象")

    lock = request.app.get("config_lock")
    if lock is None:
        lock = asyncio.Lock()
        request.app["config_lock"] = lock
    async with lock:
        next_cfg = {
            **cfg,
            "llm": dict(cfg.get("llm", {})),
            "lesson": dict(cfg.get("lesson", {})),
            "email": dict(cfg.get("email", emailer.DEFAULT_EMAIL_SETTINGS)),
            "bot": dict(cfg.get("bot", {})),
        }
        changed = False
        dry_run_changed = False
        auto_start_changed = False
        llm_changed = False
        email_changed = False
        if "auto_answer" in body and "dry_run" in body:
            raise ValueError("auto_answer 与 dry_run 不能同时设置")
        if "auto_answer" in body or "dry_run" in body:
            field = "auto_answer" if "auto_answer" in body else "dry_run"
            if not isinstance(body[field], bool):
                raise ValueError(f"{field} 必须是布尔值")
            value = not body[field] if field == "auto_answer" else body[field]
            dry_run_changed = value != next_cfg["bot"].get("dry_run")
            next_cfg["bot"]["dry_run"] = value
            changed = changed or dry_run_changed
        if "auto_start_watching" in body:
            if not isinstance(body["auto_start_watching"], bool):
                raise ValueError("auto_start_watching 必须是布尔值")
            value = body["auto_start_watching"]
            auto_start_changed = value != next_cfg["bot"].get("auto_start_watching")
            next_cfg["bot"]["auto_start_watching"] = value
            changed = changed or auto_start_changed
        if "llm" in body:
            update = body["llm"]
            if not isinstance(update, dict):
                raise ValueError("llm 配置必须是 JSON 对象")
            unknown = set(update) - {"base_url", "api_key", "model", "vision_enabled"}
            if unknown:
                raise ValueError("llm 配置包含不支持的字段")
            base_url = update.get("base_url")
            model = update.get("model")
            if not isinstance(base_url, str) or not base_url.strip():
                raise ValueError("模型 API 地址不能为空")
            if len(base_url.strip()) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in base_url):
                raise ValueError("模型 API 地址无效")
            base_url = base_url.strip().rstrip("/")
            core.chat_completions_url(base_url)
            if not isinstance(model, str) or not model.strip():
                raise ValueError("模型名称不能为空")
            if len(model.strip()) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in model):
                raise ValueError("模型名称无效")
            model = model.strip()
            requested_vision = update.get("vision_enabled")
            if requested_vision is not None and not isinstance(requested_vision, bool):
                raise ValueError("vision_enabled 必须是布尔值")
            replacement_key = update.get("api_key", "")
            if not isinstance(replacement_key, str):
                raise ValueError("API Key 必须是字符串")
            replacement_key = replacement_key.strip()
            if len(replacement_key) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in replacement_key):
                raise ValueError("API Key 无效")
            next_llm = next_cfg["llm"]
            base_url_changed = base_url != next_llm.get("base_url")
            api_changed = core.api_root_url(base_url) != core.api_root_url(
                next_llm.get("base_url", "")
            )
            if (api_changed and not replacement_key
                    and core.api_key_configured(next_llm.get("api_key", ""))):
                raise ValueError("更换模型 API 地址时必须填写对应的新 API Key")
            model_changed = model != next_llm.get("model")
            if requested_vision is None:
                requested_vision = (bool(next_llm.get("vision_enabled", False))
                                    if not base_url_changed and not model_changed else False)
            vision_changed = requested_vision != bool(next_llm.get("vision_enabled", False))
            llm_changed = base_url_changed or model_changed or vision_changed
            next_llm.update({"base_url": base_url, "model": model,
                             "vision_enabled": requested_vision})
            if replacement_key:
                llm_changed = llm_changed or replacement_key != next_llm.get("api_key")
                next_llm["api_key"] = replacement_key
            changed = changed or llm_changed

        if "email" in body:
            current_email = next_cfg["email"]
            next_email = emailer.merge_email_settings(current_email, body["email"])
            email_changed = next_email != current_email
            next_cfg["email"] = next_email
            changed = changed or email_changed

        candidate_solver = (core.LLMSolver(next_cfg, llm_session)
                            if llm_changed else solver)
        if changed:
            save_config(next_cfg)
            cfg.clear()
            cfg.update(next_cfg)
            hub.state["dry_run"] = cfg["bot"].get("dry_run", True)
            if llm_changed:
                solver = candidate_solver
                if account_manager is not None:
                    account_manager.set_solver(candidate_solver)
                elif watcher is not None:
                    watcher.solver = candidate_solver
            if email_changed and account_manager is not None:
                account_manager.set_email_config(next_cfg["email"])
            config_revision += 1
            if dry_run_changed:
                enabled = not cfg["bot"]["dry_run"]
                hub.log("自动答题 => " + ("已开启（自动提交）" if enabled else "已关闭（仅显示建议答案）"),
                        "warn" if enabled else "info")
            if auto_start_changed:
                hub.log("登录后自动监课 => " + str(cfg["bot"]["auto_start_watching"]))
            if llm_changed:
                hub.log(f"模型配置已更新: {cfg['llm']['model']}")
            if email_changed:
                hub.log("邮件通知配置已更新")
            await hub.sync_state()
        response = {"ok": True, "manual_checkin": True,
                    "revision": config_revision, "llm": public_llm_config(next_cfg),
                    "email": public_email_config(next_cfg),
                    "admin": public_admin_config(request, next_cfg),
                    "scanner": public_scanner_config(request, next_cfg)}
    return web.json_response(response, headers={"Cache-Control": "no-store"})


async def api_config_get(request):
    return web.json_response({
        "dry_run": cfg["bot"].get("dry_run", True),
        "auto_answer": not cfg["bot"].get("dry_run", True),
        "auto_start_watching": cfg["bot"].get("auto_start_watching", True),
        "revision": config_revision,
        "llm": public_llm_config(),
        "email": public_email_config(),
        "admin": public_admin_config(request),
        "scanner": public_scanner_config(request),
    }, headers={"Cache-Control": "no-store"})


async def api_email_test(request):
    request_error = _local_json_request_error(request)
    if request_error is not None:
        return request_error
    body = await request.json()
    if body != {}:
        raise ValueError("测试邮件请求不接受配置字段，请先保存邮件配置")
    notifier = (account_manager.notifier if account_manager is not None
                else emailer.EmailNotifier(cfg.get("email", {}), hub.log))
    try:
        await notifier.send_test()
    except emailer.EmailTestCooldownError as exc:
        return web.json_response({"ok": False, "message": str(exc)}, status=429)
    except emailer.EmailNotificationError as exc:
        hub.log(f"测试邮件发送失败: {exc}", "error")
        return web.json_response({"ok": False, "message": str(exc)}, status=502)
    return web.json_response({"ok": True, "message": "测试邮件已发送"},
                             headers={"Cache-Control": "no-store"})


async def api_llm_models(request):
    request_error = _local_json_request_error(request)
    if request_error is not None:
        return request_error
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError("模型列表请求必须是 JSON 对象")
    unknown = set(body) - {"base_url", "api_key"}
    if unknown:
        raise ValueError("模型列表请求包含不支持的字段")

    base_url = body.get("base_url", cfg["llm"].get("base_url", ""))
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("模型 API 地址不能为空")
    if len(base_url.strip()) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in base_url):
        raise ValueError("模型 API 地址无效")
    base_url = base_url.strip().rstrip("/")
    core.models_url(base_url)

    replacement_key = body.get("api_key", "")
    if not isinstance(replacement_key, str):
        raise ValueError("API Key 必须是字符串")
    replacement_key = replacement_key.strip()
    if len(replacement_key) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in replacement_key):
        raise ValueError("API Key 无效")

    lock = request.app.get("config_lock")
    if lock is None:
        lock = asyncio.Lock()
        request.app["config_lock"] = lock
    async with lock:
        settings = dict(cfg["llm"])
    same_api = core.api_root_url(base_url) == core.api_root_url(
        settings.get("base_url", "")
    )
    settings["base_url"] = base_url
    if replacement_key:
        settings["api_key"] = replacement_key
    elif not same_api:
        settings["api_key"] = ""
    candidate = core.LLMSolver({"llm": settings}, llm_session)
    models = await candidate.list_models()
    return web.json_response({"ok": True, "models": models},
                             headers={"Cache-Control": "no-store"})


async def on_startup(app):
    global llm_session, solver, account_manager
    llm_session = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
    solver = core.LLMSolver(cfg, llm_session)
    account_manager = AccountManager(cfg, solver, hub)
    _refresh_legacy_aliases()
    hub.state["dry_run"] = cfg["bot"].get("dry_run", True)
    if account_manager.records:
        hub.state["detail"] = "正在恢复本地账号会话"
        app["restore_task"] = asyncio.create_task(_restore_saved_session(), name="restore-session")
    else:
        await hub.sync_state()


async def _restore_saved_session():
    if account_manager is not None:
        await account_manager.restore_all()
        return
    try:
        saved = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
        if saved.get("server") and saved["server"] != client.server_key:
            raise RuntimeError("保存的账号与当前服务器不一致")
        if await client.restore_session(saved):
            hub.state.update({"user_id": client.user_id, "user_name": client.user_name,
                              "session_saved": True})
            hub.log(f"已恢复账号会话: {client.user_name or client.user_id}")
            if cfg["bot"].get("auto_start_watching", True):
                watcher.start()
            else:
                watcher.set_state("idle", "账号会话已恢复")
        else:
            hub.state["session_saved"] = False
            watcher.set_state("idle", "保存的登录态已失效，请重新扫码")
            hub.log("保存的登录态已失效，请重新扫码", "warn")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        hub.state["session_saved"] = False
        watcher.set_state("idle", "本地账号会话恢复失败，请重新扫码")
        hub.log(f"本地账号会话恢复失败: {exc}", "warn")


async def on_cleanup(app):
    await _cancel_restore(app)
    await _supersede_qr_attempt()
    if account_manager is not None:
        await account_manager.shutdown()
    elif watcher:
        await watcher.stop()
        if http_session:
            await http_session.close()
    if llm_session:
        await llm_session.close()


def make_app() -> web.Application:
    app = web.Application(middlewares=[
        security_headers_middleware, admin_auth_middleware,
        csrf_origin_middleware, api_error_middleware,
    ])
    app["runtime_config"] = server_runtime_config(config=cfg)
    app["auth_lock"] = asyncio.Lock()
    app["auth_failures"] = {}
    app["scanner_auth_lock"] = asyncio.Lock()
    app["scanner_auth_failures"] = {}
    app["scan_lock"] = asyncio.Lock()
    app["last_scan_at"] = 0.0
    app["config_lock"] = asyncio.Lock()
    app.router.add_get("/", index)
    app.router.add_get("/scan", scanner_page)
    app.router.add_static("/vendor/", ROOT / "static" / "vendor", show_index=False)
    app.router.add_get("/api/health", api_health)
    app.router.add_get("/api/auth/status", api_auth_status)
    app.router.add_post("/api/auth/login", api_auth_login)
    app.router.add_post("/api/auth/logout", api_auth_logout)
    app.router.add_post("/api/admin/credentials", api_admin_credentials)
    app.router.add_get("/api/scanner/status", api_scanner_status)
    app.router.add_post("/api/scanner/login", api_scanner_login)
    app.router.add_post("/api/scanner/logout", api_scanner_logout)
    app.router.add_post("/api/scanner/credentials", api_scanner_credentials)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/login/qr/start", api_login_qr_start)
    app.router.add_post("/api/login/qr/poll", api_login_qr_poll)
    app.router.add_post("/api/login/cookie", api_login_cookie)
    app.router.add_post("/api/logout", api_logout)
    app.router.add_post("/api/watch/start", api_watch_start)
    app.router.add_post("/api/watch/stop", api_watch_stop)
    app.router.add_post("/api/watch/join", api_watch_join)
    app.router.add_post("/api/accounts/{account_id}/watch/start", api_account_watch_start)
    app.router.add_post("/api/accounts/{account_id}/watch/stop", api_account_watch_stop)
    app.router.add_post("/api/accounts/{account_id}/watch/join", api_account_watch_join)
    app.router.add_post("/api/accounts/{account_id}/remove", api_account_remove)
    app.router.add_post("/api/accounts/scan-all", api_accounts_scan_all)
    app.router.add_get("/api/config", api_config_get)
    app.router.add_post("/api/config", api_config)
    app.router.add_post("/api/config/llm/models", api_llm_models)
    app.router.add_post("/api/config/email/test", api_email_test)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    runtime = server_runtime_config(config=cfg)
    display_host = "127.0.0.1" if runtime["host"] in ("0.0.0.0", "::") else runtime["host"]
    print(f"雨课堂自动答题面板: http://{display_host}:{runtime['port']}")
    web.run_app(make_app(), host=runtime["host"], port=runtime["port"], print=None)
