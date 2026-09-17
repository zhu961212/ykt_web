import asyncio
import json
import smtplib
import sys
import unittest
from pathlib import Path
from unittest import mock


MODULE_DIR = Path(__file__).resolve().parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

import email_notifier as emailer


def valid_settings(*, security="ssl"):
    return {
        "enabled": True,
        "smtp_host": "smtp.example.com",
        "smtp_port": 465 if security == "ssl" else 587,
        "security": security,
        "username": "sender@example.com",
        "password": "mail-secret",
        "from_address": "sender@example.com",
        "to_address": "owner@example.com",
        "cooldown_seconds": 60,
    }


class EmailSettingsTests(unittest.TestCase):
    def test_public_settings_redact_password(self):
        public = emailer.public_email_settings(valid_settings())

        self.assertEqual(set(public), {
            "enabled", "smtp_host", "smtp_port", "security", "username",
            "from_address", "to_address", "cooldown_seconds",
            "password_configured",
        })
        self.assertTrue(public["password_configured"])
        self.assertNotIn("password", public)
        self.assertNotIn("mail-secret", json.dumps(public))

    def test_blank_password_preserves_secret_when_connection_identity_is_unchanged(self):
        merged = emailer.merge_email_settings(valid_settings(), {
            "password": "",
            "to_address": "backup@example.com",
            "cooldown_seconds": 120,
        })

        self.assertEqual(merged["password"], "mail-secret")
        self.assertEqual(merged["to_address"], "backup@example.com")
        self.assertEqual(merged["cooldown_seconds"], 120)

    def test_connection_identity_change_requires_new_password(self):
        cases = (
            ("smtp_host", "smtp2.example.com"),
            ("smtp_port", 2465),
            ("security", "starttls"),
            ("username", "other@example.com"),
        )
        for field, value in cases:
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    emailer.merge_email_settings(valid_settings(), {
                        field: value,
                        "password": "",
                    })

    def test_connection_identity_change_accepts_replacement_password(self):
        merged = emailer.merge_email_settings(valid_settings(), {
            "smtp_host": "smtp2.example.com",
            "password": "replacement-secret",
        })

        self.assertEqual(merged["smtp_host"], "smtp2.example.com")
        self.assertEqual(merged["password"], "replacement-secret")

    def test_invalid_fields_ports_addresses_and_plaintext_modes_are_rejected(self):
        invalid_updates = (
            {"unexpected": True},
            {"smtp_port": 0},
            {"smtp_port": 65536},
            {"smtp_port": True},
            {"smtp_port": "465"},
            {"from_address": "not-an-address"},
            {"to_address": "missing-domain"},
            {"to_address": "owner@example.com\nBcc: attacker@example.com"},
            {"security": "plain"},
            {"security": "none"},
        )
        for update in invalid_updates:
            with self.subTest(update=update):
                with self.assertRaises(ValueError):
                    emailer.merge_email_settings(
                        emailer.DEFAULT_EMAIL_SETTINGS, update,
                    )


class SMTPTransportTests(unittest.TestCase):
    def test_ssl_transport_logs_in_and_sends_without_starttls(self):
        settings = valid_settings(security="ssl")
        smtp = mock.MagicMock()
        context = object()

        with mock.patch.object(emailer.ssl, "create_default_context", return_value=context), \
                mock.patch.object(emailer.smtplib, "SMTP_SSL", return_value=smtp) as smtp_ssl, \
                mock.patch.object(emailer.smtplib, "SMTP") as smtp_plain:
            emailer.EmailNotifier._send_sync(settings, "Alert", "Details")

        smtp_ssl.assert_called_once_with(
            "smtp.example.com", 465, timeout=15, context=context,
        )
        smtp_plain.assert_not_called()
        smtp.starttls.assert_not_called()
        smtp.login.assert_called_once_with("sender@example.com", "mail-secret")
        smtp.send_message.assert_called_once()
        call_names = [call[0] for call in smtp.method_calls]
        self.assertLess(call_names.index("login"), call_names.index("send_message"))
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["Subject"], "Alert")
        self.assertEqual(message["From"], "sender@example.com")
        self.assertEqual(message["To"], "owner@example.com")
        self.assertIn("Details", message.get_content())

    def test_starttls_transport_upgrades_before_login_and_send(self):
        settings = valid_settings(security="starttls")
        smtp = mock.MagicMock()
        context = object()

        with mock.patch.object(emailer.ssl, "create_default_context", return_value=context), \
                mock.patch.object(emailer.smtplib, "SMTP", return_value=smtp) as smtp_plain, \
                mock.patch.object(emailer.smtplib, "SMTP_SSL") as smtp_ssl:
            emailer.EmailNotifier._send_sync(settings, "Alert", "Details")

        smtp_plain.assert_called_once_with("smtp.example.com", 587, timeout=15)
        smtp_ssl.assert_not_called()
        self.assertEqual(smtp.ehlo.call_count, 2)
        smtp.starttls.assert_called_once_with(context=context)
        smtp.login.assert_called_once_with("sender@example.com", "mail-secret")
        smtp.send_message.assert_called_once()
        call_names = [call[0] for call in smtp.method_calls]
        self.assertLess(call_names.index("starttls"), call_names.index("login"))
        self.assertLess(call_names.index("login"), call_names.index("send_message"))


class EmailNotifierAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_notification_and_test_email_ignore_low_monotonic_origin(self):
        notifier = emailer.EmailNotifier(valid_settings())
        notifier._deliver = mock.AsyncMock()
        clock = mock.Mock()
        clock.monotonic.side_effect = (1.0, 1.0)

        with mock.patch.object(emailer, "time", clock):
            notified = await notifier.notify(
                "account:authorization", "Alert", "First notification",
            )
            tested = await notifier.send_test()

        self.assertTrue(notified)
        self.assertTrue(tested)
        self.assertEqual(notifier._deliver.await_count, 2)

    async def test_notify_deduplicates_same_key_until_cooldown_expires(self):
        notifier = emailer.EmailNotifier(valid_settings())
        notifier._deliver = mock.AsyncMock()
        clock = mock.Mock()
        clock.monotonic.side_effect = (1000.0, 1010.0, 1061.0)

        with mock.patch.object(emailer, "time", clock):
            first = await notifier.notify("account:authorization", "Alert", "First")
            duplicate = await notifier.notify("account:authorization", "Alert", "Second")
            after_cooldown = await notifier.notify(
                "account:authorization", "Alert", "Third",
            )

        self.assertTrue(first)
        self.assertFalse(duplicate)
        self.assertTrue(after_cooldown)
        self.assertEqual(notifier._deliver.await_count, 2)
        self.assertEqual(
            [call.args[2] for call in notifier._deliver.await_args_list],
            ["First", "Third"],
        )

    async def test_test_email_has_independent_thirty_second_cooldown(self):
        notifier = emailer.EmailNotifier(valid_settings())
        notifier._deliver = mock.AsyncMock()
        clock = mock.Mock()
        clock.monotonic.side_effect = (100.0, 101.0, 131.0)

        with mock.patch.object(emailer, "time", clock):
            self.assertTrue(await notifier.send_test())
            with self.assertRaises(emailer.EmailTestCooldownError):
                await notifier.send_test()
            self.assertTrue(await notifier.send_test())

        self.assertEqual(notifier._deliver.await_count, 2)
        for call in notifier._deliver.await_args_list:
            self.assertIn("邮件通知测试", call.args[1])

    async def test_smtp_authentication_errors_do_not_leak_password(self):
        secret = "mail-secret-do-not-leak"
        settings = valid_settings()
        settings["password"] = secret
        logs = []
        notifier = emailer.EmailNotifier(
            settings, lambda text, level="info": logs.append((level, text)),
        )
        upstream_error = smtplib.SMTPAuthenticationError(
            535, f"authentication rejected: {secret}".encode(),
        )
        clock = mock.Mock()
        clock.monotonic.return_value = 1000.0

        with mock.patch.object(
                emailer.EmailNotifier, "_send_sync", side_effect=upstream_error), \
                mock.patch.object(emailer, "time", clock):
            sent = await notifier.notify("account:authorization", "Alert", "Details")

        self.assertFalse(sent)
        rendered_logs = json.dumps(logs, ensure_ascii=False)
        self.assertNotIn(secret, rendered_logs)
        self.assertIn("SMTP", rendered_logs)

        second = emailer.EmailNotifier(settings)
        test_clock = mock.Mock()
        test_clock.monotonic.return_value = 100.0
        with mock.patch.object(
                emailer.EmailNotifier, "_send_sync", side_effect=upstream_error), \
                mock.patch.object(emailer, "time", test_clock):
            with self.assertRaises(emailer.EmailNotificationError) as raised:
                await second.send_test()

        self.assertNotIn(secret, str(raised.exception))
        self.assertIn("SMTP", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
