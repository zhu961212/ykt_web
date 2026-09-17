# -*- coding: utf-8 -*-
"""Validated SMTP email notifications with async deduplication."""

import asyncio
import smtplib
import ssl
import time
from email.headerregistry import Address
from email.message import EmailMessage


DEFAULT_EMAIL_SETTINGS = {
    "enabled": False,
    "smtp_host": "",
    "smtp_port": 465,
    "security": "ssl",
    "username": "",
    "password": "",
    "from_address": "",
    "to_address": "",
    "cooldown_seconds": 900,
}

EMAIL_FIELDS = set(DEFAULT_EMAIL_SETTINGS)
CONNECTION_FIELDS = {"smtp_host", "smtp_port", "security", "username"}


class EmailNotificationError(RuntimeError):
    pass


class EmailTestCooldownError(EmailNotificationError):
    pass


def _clean_text(value, label, max_length, *, required=False):
    if not isinstance(value, str):
        raise ValueError(f"{label} 必须是字符串")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{label} 不能为空")
    if len(value) > max_length or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{label} 无效")
    return value


def _clean_address(value, label, *, required=False):
    value = _clean_text(value, label, 320, required=required)
    if not value:
        return ""
    try:
        address = Address(addr_spec=value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 无效") from exc
    if not address.username or not address.domain:
        raise ValueError(f"{label} 无效")
    return address.addr_spec


def merge_email_settings(current, update):
    if not isinstance(update, dict):
        raise ValueError("email 配置必须是 JSON 对象")
    unknown = set(update) - EMAIL_FIELDS
    if unknown:
        raise ValueError("email 配置包含不支持的字段")

    previous = {**DEFAULT_EMAIL_SETTINGS, **(current or {})}
    merged = dict(previous)

    if "enabled" in update:
        if not isinstance(update["enabled"], bool):
            raise ValueError("enabled 必须是布尔值")
        merged["enabled"] = update["enabled"]

    if "smtp_port" in update:
        port = update["smtp_port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("SMTP 端口必须是 1-65535 的整数")
        merged["smtp_port"] = port

    if "cooldown_seconds" in update:
        cooldown = update["cooldown_seconds"]
        if (isinstance(cooldown, bool) or not isinstance(cooldown, int)
                or not 60 <= cooldown <= 86400):
            raise ValueError("通知间隔必须是 60-86400 秒的整数")
        merged["cooldown_seconds"] = cooldown

    if "security" in update:
        security = _clean_text(update["security"], "SMTP 加密方式", 20, required=True)
        if security not in ("ssl", "starttls"):
            raise ValueError("SMTP 加密方式仅支持 ssl 或 starttls")
        merged["security"] = security

    if "smtp_host" in update:
        host = _clean_text(update["smtp_host"], "SMTP 地址", 253)
        if host and (any(char.isspace() for char in host) or any(char in host for char in "/\\@")):
            raise ValueError("SMTP 地址无效")
        merged["smtp_host"] = host
    if "username" in update:
        merged["username"] = _clean_text(update["username"], "SMTP 用户名", 320)
    if "from_address" in update:
        merged["from_address"] = _clean_address(update["from_address"], "发件地址")
    if "to_address" in update:
        merged["to_address"] = _clean_address(update["to_address"], "收件地址")

    replacement_password = update.get("password")
    if replacement_password is not None:
        if not isinstance(replacement_password, str):
            raise ValueError("SMTP 密码必须是字符串")
        if (len(replacement_password) > 8192
                or any(ord(char) < 32 or ord(char) == 127 for char in replacement_password)):
            raise ValueError("SMTP 密码无效")
        if replacement_password:
            merged["password"] = replacement_password

    identity_changed = any(merged[field] != previous[field] for field in CONNECTION_FIELDS)
    if identity_changed and previous.get("password") and not replacement_password:
        raise ValueError("更换 SMTP 连接信息时必须填写新的 SMTP 密码")

    if merged["enabled"]:
        required = {
            "smtp_host": "SMTP 地址",
            "username": "SMTP 用户名",
            "password": "SMTP 密码",
            "from_address": "发件地址",
            "to_address": "收件地址",
        }
        for field, label in required.items():
            if not merged.get(field):
                raise ValueError(f"启用邮件通知时 {label} 不能为空")
    return merged


def public_email_settings(settings):
    values = {**DEFAULT_EMAIL_SETTINGS, **(settings or {})}
    return {
        "enabled": bool(values.get("enabled", False)),
        "smtp_host": str(values.get("smtp_host") or ""),
        "smtp_port": int(values.get("smtp_port") or 465),
        "security": str(values.get("security") or "ssl"),
        "username": str(values.get("username") or ""),
        "from_address": str(values.get("from_address") or ""),
        "to_address": str(values.get("to_address") or ""),
        "cooldown_seconds": int(values.get("cooldown_seconds") or 900),
        "password_configured": bool(values.get("password")),
    }


def email_settings_ready(settings):
    values = {**DEFAULT_EMAIL_SETTINGS, **(settings or {})}
    return all(values.get(field) for field in (
        "smtp_host", "username", "password", "from_address", "to_address",
    ))


class EmailNotifier:
    def __init__(self, settings=None, logger=None):
        self._settings = merge_email_settings(DEFAULT_EMAIL_SETTINGS, settings or {})
        self._logger = logger
        self._last_sent = {}
        self._last_failed = {}
        self._inflight = {}
        self._key_epochs = {}
        self._generation = 0
        self._retry_tasks = {}
        self._closed = False
        self._dedupe_lock = asyncio.Lock()
        self._test_lock = asyncio.Lock()
        self._last_test = None

    @property
    def settings(self):
        return dict(self._settings)

    def configure(self, settings):
        self._generation += 1
        self._settings = merge_email_settings(DEFAULT_EMAIL_SETTINGS, settings or {})
        self._last_sent.clear()
        self._last_failed.clear()
        self._inflight.clear()
        self._key_epochs.clear()
        for task in self._retry_tasks.values():
            task.cancel()
        self._retry_tasks.clear()

    def _log(self, text, level="info"):
        if self._logger is not None:
            try:
                self._logger(text, level)
            except Exception:
                pass

    @staticmethod
    def _error_message(exc):
        if isinstance(exc, smtplib.SMTPAuthenticationError):
            return "SMTP 认证失败，请检查用户名和授权码"
        if isinstance(exc, smtplib.SMTPRecipientsRefused):
            return "SMTP 服务器拒绝了收件地址"
        if isinstance(exc, ssl.SSLError):
            return "SMTP TLS 连接失败"
        if isinstance(exc, (TimeoutError, OSError)):
            return "SMTP 服务器连接失败"
        if isinstance(exc, smtplib.SMTPException):
            return "SMTP 服务器返回错误"
        return "邮件发送失败"

    @staticmethod
    def _send_sync(settings, subject, body):
        message = EmailMessage()
        message["Subject"] = " ".join(str(subject).splitlines())[:160]
        message["From"] = settings["from_address"]
        message["To"] = settings["to_address"]
        message.set_content(str(body)[:20000])

        context = ssl.create_default_context()
        if settings["security"] == "ssl":
            smtp = smtplib.SMTP_SSL(
                settings["smtp_host"], settings["smtp_port"], timeout=15, context=context,
            )
        else:
            smtp = smtplib.SMTP(settings["smtp_host"], settings["smtp_port"], timeout=15)
        with smtp:
            if settings["security"] == "starttls":
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
            smtp.login(settings["username"], settings["password"])
            smtp.send_message(message)

    async def _deliver(self, settings, subject, body):
        try:
            await asyncio.to_thread(self._send_sync, settings, subject, body)
        except Exception as exc:
            raise EmailNotificationError(self._error_message(exc)) from exc

    def _schedule_retry(self, key, subject, body, generation, key_epoch):
        existing = self._retry_tasks.get(key)
        if (self._closed or generation != self._generation
                or key_epoch != self._key_epochs.get(key, 0)
                or (existing is not None and not existing.done())):
            return
        task = asyncio.create_task(
            self._retry_notice(key, subject, body, generation, key_epoch),
            name=f"email-retry-{key}",
        )
        self._retry_tasks[key] = task
        task.add_done_callback(lambda completed, retry_key=key: self._retry_done(retry_key, completed))

    def _retry_done(self, key, task):
        if self._retry_tasks.get(key) is task:
            self._retry_tasks.pop(key, None)
        if not task.cancelled():
            task.exception()

    async def _retry_notice(self, key, subject, body, generation, key_epoch):
        for delay in (65, 300):
            await asyncio.sleep(delay)
            if (self._closed or generation != self._generation
                    or key_epoch != self._key_epochs.get(key, 0)):
                return
            if await self.notify(
                key, subject, body, _schedule_retry=False,
                _expected_generation=generation, _expected_key_epoch=key_epoch,
            ):
                return

    async def notify(self, key, subject, body, _schedule_retry=True,
                     _expected_generation=None, _expected_key_epoch=None):
        key = str(key)
        now = time.monotonic()
        async with self._dedupe_lock:
            settings = self.settings
            generation = self._generation
            key_epoch = self._key_epochs.get(key, 0)
            attempt = (generation, key_epoch)
            if (self._closed
                    or (_expected_generation is not None
                        and generation != _expected_generation)
                    or (_expected_key_epoch is not None
                        and key_epoch != _expected_key_epoch)
                    or not settings.get("enabled")
                    or not email_settings_ready(settings)):
                return False
            if key in self._inflight:
                return False
            previous = self._last_sent.get(key)
            if previous is not None and now - previous < settings["cooldown_seconds"]:
                return False
            failed = self._last_failed.get(key)
            if failed is not None and now - failed < min(60, settings["cooldown_seconds"]):
                return False
            self._inflight[key] = attempt
        try:
            await self._deliver(settings, subject, body)
        except EmailNotificationError as exc:
            async with self._dedupe_lock:
                if self._inflight.get(key) == attempt:
                    self._inflight.pop(key, None)
                if (generation != self._generation
                        or key_epoch != self._key_epochs.get(key, 0)):
                    return False
                self._last_failed[key] = now
            self._log(f"邮件通知发送失败: {exc}", "error")
            if _schedule_retry:
                self._schedule_retry(key, subject, body, generation, key_epoch)
            return False
        except asyncio.CancelledError:
            async with self._dedupe_lock:
                if self._inflight.get(key) == attempt:
                    self._inflight.pop(key, None)
            raise
        async with self._dedupe_lock:
            if self._inflight.get(key) == attempt:
                self._inflight.pop(key, None)
            if (generation != self._generation
                    or key_epoch != self._key_epochs.get(key, 0)):
                return False
            self._last_failed.pop(key, None)
            self._last_sent[key] = now
        self._log(f"邮件通知已发送: {subject}")
        return True

    async def close(self):
        self._closed = True
        self._generation += 1
        self._inflight.clear()
        tasks = [task for task in self._retry_tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._retry_tasks.clear()

    def clear(self, key):
        key = str(key)
        self._key_epochs[key] = self._key_epochs.get(key, 0) + 1
        self._inflight.pop(key, None)
        task = self._retry_tasks.pop(key, None)
        if task is not None:
            task.cancel()
        self._last_sent.pop(key, None)
        self._last_failed.pop(key, None)

    def clear_prefix(self, prefix):
        prefix = str(prefix)
        keys = (set(self._retry_tasks) | set(self._last_sent)
                | set(self._last_failed) | set(self._inflight))
        for key in keys:
            if key.startswith(prefix):
                self.clear(key)

    async def send_test(self):
        settings = self.settings
        if not email_settings_ready(settings):
            raise EmailNotificationError("邮件配置不完整，请先保存 SMTP 设置")
        async with self._test_lock:
            now = time.monotonic()
            if self._last_test is not None and now - self._last_test < 30:
                raise EmailTestCooldownError("测试邮件发送过于频繁，请稍后再试")
            self._last_test = now
            try:
                await self._deliver(
                    settings,
                    "[雨课堂答题面板] 邮件通知测试",
                    "邮件通知配置有效。收到此邮件表示账号异常告警可以正常送达。",
                )
            except EmailNotificationError:
                raise
        self._log("测试邮件已发送")
        return True
