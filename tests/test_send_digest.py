import json
import smtplib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import send_digest as mail


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = mail.MailSettings(
            "smtp.example.invalid", 465, "ssl", "sender@example.invalid", "test-password",
            "sender@example.invalid", ["one@example.invalid", "two@example.invalid"],
            "https://example.invalid/", 20, 0, ["arxiv", "prb"], [],
        )
        self.payload = {"generated_at": datetime.now(timezone.utc).isoformat(), "papers": [
            {"source": "arxiv", "title": "Measured spin correlations", "authors": ["A. Author"],
             "abs_url": "https://arxiv.org/abs/2610.00001", "summary_mode": "rule-based",
             "abstract_summary_zh": "A measured conclusion.", "main_content_zh": "Distinct evidence."},
        ]}
        self.state = self.root / "ledger.json"

    def test_qq_defaults_and_multiple_recipients(self):
        with patch.dict(mail.os.environ, {"SMTP_USERNAME": "sender@qq.com", "SMTP_PASSWORD": "test", "MAIL_TO": "a@qq.com;b@example.invalid,a@qq.com"}, clear=True):
            settings = mail.settings_from_env({"email": {"site_url": "https://example.invalid/"}})
        self.assertEqual((settings.host, settings.port, settings.security), ("smtp.qq.com", 465, "ssl"))
        self.assertEqual(len(settings.recipients), 2)

    def test_custom_smtp_and_starttls_are_supported(self):
        with patch.dict(mail.os.environ, {"SMTP_USERNAME": "sender@example.invalid", "SMTP_PASSWORD": "test", "MAIL_TO": "one@example.invalid", "SMTP_HOST": "smtp.other.invalid", "SMTP_PORT": "587", "SMTP_SECURITY": "starttls"}, clear=True):
            settings = mail.settings_from_env({"email": {"site_url": "https://example.invalid/"}})
        self.assertEqual(settings.host, "smtp.other.invalid")
        self.assertEqual((settings.port, settings.security), (587, "starttls"))

    def test_switching_security_selects_matching_default_port(self):
        with patch.dict(mail.os.environ, {"SMTP_SECURITY": "starttls"}, clear=True):
            settings = mail.settings_from_env({"email": {"smtp_security": "ssl", "smtp_port": 465, "site_url": "https://example.invalid/"}}, preview=True)
        self.assertEqual(settings.port, 587)

    def test_old_arxiv_http_links_are_upgraded_to_https(self):
        self.payload["papers"][0]["abs_url"] = "http://arxiv.org/abs/2610.00001"
        _, text, _ = mail.build_digest(self.payload, self.settings)
        self.assertIn("https://arxiv.org/abs/2610.00001", text)

    def test_authentication_failure_stops_group_without_retry(self):
        with patch.object(mail, "deliver", side_effect=smtplib.SMTPAuthenticationError(535, b"bad login")) as send:
            report = mail.send_group(self.payload, self.settings, self.state)
        send.assert_called_once()
        self.assertEqual(report["failed"], 2)

    def test_missing_secrets_are_reported_without_values(self):
        with patch.dict(mail.os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "SMTP_USERNAME.*SMTP_PASSWORD.*MAIL_TO"):
                mail.settings_from_env({})

    def test_plaintext_smtp_is_rejected(self):
        with patch.dict(mail.os.environ, {"SMTP_SECURITY": "none"}, clear=True):
            with self.assertRaisesRegex(ValueError, "plaintext"):
                mail.settings_from_env({"email": {"site_url": "https://example.invalid/"}}, preview=True)

    def test_invalid_recipient_is_rejected(self):
        with self.assertRaises(ValueError):
            mail.addresses("not-an-email")

    def test_per_recipient_message_does_not_expose_others(self):
        message = mail.build_message("日报", "text", "<p>html</p>", self.settings, self.settings.recipients[0], "2026-10-08")
        self.assertEqual(message["To"], self.settings.recipients[0])
        self.assertNotIn(self.settings.recipients[1], message.as_string())
        self.assertIsNone(message["Bcc"])

    def test_digest_escapes_html_and_has_corresponding_author_note(self):
        self.payload["papers"][0]["title"] = '<img src=x onerror="attack()">'
        _, text, html = mail.build_digest(self.payload, self.settings)
        self.assertNotIn("<img", html)
        self.assertIn("corresponding author not confirmed", text)
        self.assertIn('name="viewport"', html)
        self.assertIn("overflow-wrap:anywhere", html)

    def test_selection_balances_sources_and_excludes_stale(self):
        self.settings.max_papers = 2
        self.payload["papers"] += [dict(self.payload["papers"][0]), {"source": "prb", "title": "PRB paper"}]
        selected, matched = mail.select_papers(self.payload, self.settings)
        self.assertEqual([paper["source"] for paper in selected], ["arxiv", "prb"])
        self.assertEqual(matched, 3)
        self.payload["source_status"] = {"prb": {"status": "stale"}}
        self.assertEqual(mail.select_papers(self.payload, self.settings)[1], 2)

    def test_successful_recipients_are_not_sent_twice(self):
        with patch.object(mail, "deliver") as send:
            first = mail.send_group(self.payload, self.settings, self.state)
            second = mail.send_group(self.payload, self.settings, self.state)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(first["accepted"], 2)
        self.assertEqual(second["skipped"], 2)
        self.assertNotIn("@", self.state.read_text())

    def test_partial_failure_retries_only_failed_recipient(self):
        with patch.object(mail, "deliver", side_effect=[None, smtplib.SMTPRecipientsRefused({"recipient": (550, b"bad")})]):
            first = mail.send_group(self.payload, self.settings, self.state)
        with patch.object(mail, "deliver") as send:
            second = mail.send_group(self.payload, self.settings, self.state)
        self.assertEqual(first["failed"], 1)
        self.assertEqual(second["accepted"], 1)
        self.assertEqual(second["skipped"], 1)
        send.assert_called_once()

    def test_uncertain_delivery_is_not_automatically_retried(self):
        with patch.object(mail, "deliver", side_effect=mail.DeliveryUncertain("uncertain")) as send:
            mail.send_group(self.payload, self.settings, self.state)
            second = mail.send_group(self.payload, self.settings, self.state)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(second["uncertain"], 2)

    def test_stale_data_and_broken_ledger_fail_closed(self):
        self.payload["generated_at"] = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        with patch.object(mail, "deliver") as send:
            with self.assertRaisesRegex(ValueError, "outdated"):
                mail.send_group(self.payload, self.settings, self.state)
            self.state.write_text("[]")
            with self.assertRaisesRegex(ValueError, "ledger"):
                mail.send_group(self.payload, self.settings, self.state)
        send.assert_not_called()

    def test_smtp_tls_is_established_before_login(self):
        self.settings.security = "starttls"
        server = MagicMock()
        server.send_message.return_value = {}
        with patch.object(mail.smtplib, "SMTP", return_value=server):
            mail.deliver(mail.build_message("subject", "text", "html", self.settings, "one@example.invalid", "day"), self.settings)
        names = [call[0] for call in server.mock_calls]
        self.assertLess(names.index("starttls"), names.index("login"))

    def test_disconnect_during_data_is_ambiguous_and_not_retried(self):
        server = MagicMock()
        server.send_message.side_effect = smtplib.SMTPServerDisconnected("missing acknowledgement")
        with patch.object(mail.smtplib, "SMTP_SSL", return_value=server) as connect:
            with self.assertRaises(mail.DeliveryUncertain):
                mail.deliver(mail.build_message("subject", "text", "html", self.settings, "one@example.invalid", "day"), self.settings)
        connect.assert_called_once()

    def test_dry_run_never_contacts_smtp_or_changes_ledger(self):
        config = self.root / "config.json"
        config.write_text(json.dumps({"email": {"site_url": "https://example.invalid/"}}))
        data = self.root / "latest.json"
        data.write_text(json.dumps(self.payload))
        args = ["send_digest.py", "--dry-run", "--config", str(config), "--data", str(data), "--state", str(self.state), "--preview-dir", str(self.root / "preview"), "--report", str(self.root / "status.json")]
        with patch.dict(mail.os.environ, {}, clear=True):
            with patch.object(sys, "argv", args):
                with patch.object(mail.smtplib, "SMTP_SSL") as connect:
                    self.assertEqual(mail.main(), 0)
        connect.assert_not_called()
        self.assertFalse(self.state.exists())
        self.assertTrue((self.root / "preview/digest.html").exists())


if __name__ == "__main__":
    unittest.main()
