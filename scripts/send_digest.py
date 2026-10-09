from __future__ import annotations

import argparse
import hashlib
import json
import os
import smtplib
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, getaddresses
from html import escape
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
BEIJING = timezone(timedelta(hours=8))


class DeliveryUncertain(RuntimeError):
    pass


def addresses(raw: str) -> list[str]:
    result = []
    for _, value in getaddresses([raw.replace(";", ",").replace("\n", ",")]):
        try:
            parsed = Address(addr_spec=value.strip())
        except ValueError as exc:
            raise ValueError("MAIL_TO contains an invalid address") from exc
        if not parsed.username or not parsed.domain or "\r" in value:
            raise ValueError("MAIL_TO contains an invalid address")
        if value.lower() not in {address.lower() for address in result}:
            result.append(value)
    if not result:
        raise ValueError("MAIL_TO is empty")
    return result


@dataclass
class MailSettings:
    host: str
    port: int
    security: str
    username: str
    password: str
    sender: str
    recipients: list[str]
    site_url: str
    max_papers: int
    interval: float
    sources: list[str]
    keywords: list[str]


def settings_from_env(config: dict, preview: bool = False) -> MailSettings:
    cfg = config.get("email", {})
    username = os.environ.get("SMTP_USERNAME", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    recipient_text = os.environ.get("MAIL_TO", "")
    if not preview:
        missing = [name for name, value in (("SMTP_USERNAME", username), ("SMTP_PASSWORD", password), ("MAIL_TO", recipient_text)) if not value]
        if missing:
            raise ValueError("Missing GitHub Secrets: " + ", ".join(missing))
    sender = os.environ.get("MAIL_FROM", "").strip() or username or "preview@example.invalid"
    parsed_sender = addresses(sender)
    if len(parsed_sender) != 1:
        raise ValueError("MAIL_FROM must be one address")
    recipients = addresses(recipient_text or "preview@example.invalid")
    if len(recipients) > int(cfg.get("max_recipients", 50)):
        raise ValueError("MAIL_TO exceeds configured max_recipients")
    security = os.environ.get("SMTP_SECURITY", "") or cfg.get("smtp_security", "ssl")
    if security not in {"ssl", "starttls"}:
        raise ValueError("SMTP_SECURITY must be ssl or starttls; plaintext is not supported")
    default_port = cfg.get("smtp_port", 465 if security == "ssl" else 587)
    if security != cfg.get("smtp_security", "ssl"):
        default_port = 465 if security == "ssl" else 587
    port = int(os.environ.get("SMTP_PORT", "") or default_port)
    if not 1 <= port <= 65535:
        raise ValueError("Invalid SMTP_PORT")
    site_url = os.environ.get("SITE_URL", "") or cfg.get("site_url", "")
    if urlparse(site_url).scheme != "https" or not urlparse(site_url).netloc:
        raise ValueError("email.site_url must be an HTTPS URL")
    return MailSettings(
        host=os.environ.get("SMTP_HOST", "") or cfg.get("smtp_host", "smtp.qq.com"),
        port=port, security=security, username=username, password=password,
        sender=parsed_sender[0], recipients=recipients, site_url=site_url,
        max_papers=max(1, min(100, int(cfg.get("max_papers", 20)))),
        interval=max(0, float(cfg.get("interval_seconds", 2))),
        sources=cfg.get("sources", ["arxiv", "prb"]), keywords=cfg.get("keywords", []),
    )


def select_papers(payload: dict, settings: MailSettings) -> tuple[list[dict], int]:
    groups = {source: [] for source in settings.sources}
    for paper in payload.get("papers", []):
        source = paper.get("source", "arxiv")
        if source not in groups or payload.get("source_status", {}).get(source, {}).get("status", "ok") != "ok":
            continue
        haystack = " ".join([paper.get("title", ""), paper.get("abstract", ""), " ".join(paper.get("keywords", []))]).lower()
        if settings.keywords and not any(str(word).lower() in haystack for word in settings.keywords):
            continue
        groups[source].append(paper)
    matched = sum(len(group) for group in groups.values())
    selected = []
    for index in range(max((len(group) for group in groups.values()), default=0)):
        for group in groups.values():
            if index < len(group):
                selected.append(group[index])
                if len(selected) >= settings.max_papers:
                    return selected, matched
    return selected, matched


def build_digest(payload: dict, settings: MailSettings) -> tuple[str, str, str]:
    papers, matched = select_papers(payload, settings)
    generated = datetime.fromisoformat(payload["generated_at"].replace("Z", "+00:00")).astimezone(BEIJING)
    subject = f"凝聚态论文日报 {generated:%Y-%m-%d} | {matched} 篇"
    lines = [subject, f"更新：{generated:%Y-%m-%d %H:%M} 北京时间", f"本邮件节选 {len(papers)} 篇；完整结果：{settings.site_url}", ""]
    blocks = [f'<h1 style="font-size:22px">{escape(subject)}</h1>',
              f'<p>节选 {len(papers)} / {matched} 篇 · <a href="{escape(settings.site_url, quote=True)}">完整日报</a></p>']
    for source, status in payload.get("source_status", {}).items():
        if status.get("status") != "ok":
            warning = f"{source.upper()} 本次未更新，旧数据不纳入邮件。"
            lines.append(warning)
            blocks.append(f"<p>{escape(warning)}</p>")
    for index, paper in enumerate(papers, 1):
        label = "AI 导读" if paper.get("summary_mode", "").startswith("llm-") else "规则摘录"
        basis = "RSS 摘要片段" if paper.get("source") == "prb" else "arXiv 摘要"
        url = paper.get("abs_url", "")
        if urlparse(url).scheme == "http" and urlparse(url).netloc == "arxiv.org":
            url = url.replace("http://arxiv.org/", "https://arxiv.org/", 1)
        url = url if urlparse(url).scheme == "https" else settings.site_url
        title = paper.get("title", "")
        authors = ", ".join(paper.get("authors", [])[:4])
        corresponding = paper.get("corresponding_author") or "corresponding author not confirmed"
        finding = paper.get("abstract_summary_zh", "")[:500]
        evidence = paper.get("main_content_zh", "")[:500]
        lines.extend([f"{index}. {title} [{basis} / {label}]", authors,
                      f"通讯作者：{corresponding}", finding, evidence, url, ""])
        blocks.append(
            '<section style="border-top:1px solid #ddd;padding:16px 0">'
            f'<h2 style="font-size:17px"><a href="{escape(url, quote=True)}">{index}. {escape(title)}</a></h2>'
            f'<p style="color:#666;font-size:12px">{escape(authors)} · {basis} · {label}<br>通讯作者：{escape(corresponding)}</p>'
            f'<p>{escape(finding)}</p><p style="white-space:pre-line">{escape(evidence)}</p></section>'
        )
    html = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
            '<body style="font-family:Arial,sans-serif;line-height:1.6;max-width:800px;'
            'margin:auto;padding:20px;overflow-wrap:anywhere">' + "".join(blocks) + "</body></html>")
    return subject, "\n".join(lines), html


def build_message(subject: str, text: str, html: str, settings: MailSettings, recipient: str, day: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = settings.sender
    message["To"] = recipient
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False)
    identity = hashlib.sha256(f"{settings.sender}|{recipient.lower()}|{day}|{settings.site_url}".encode()).hexdigest()[:32]
    message["Message-ID"] = f"<{identity}@{settings.sender.rsplit('@', 1)[-1]}>"
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    return message


def deliver(message: EmailMessage, settings: MailSettings) -> None:
    for attempt in range(3):
        server = None
        stage = "connect"
        try:
            context = ssl.create_default_context()
            if settings.security == "ssl":
                server = smtplib.SMTP_SSL(settings.host, settings.port, timeout=30, context=context)
            else:
                server = smtplib.SMTP(settings.host, settings.port, timeout=30)
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
            server.login(settings.username, settings.password)
            stage = "data"
            refused = server.send_message(message, from_addr=settings.sender, to_addrs=[str(message["To"])])
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
            return
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused):
            raise
        except smtplib.SMTPResponseException as exc:
            if not 400 <= exc.smtp_code < 500 or attempt == 2:
                raise
        except (OSError, smtplib.SMTPServerDisconnected) as exc:
            if stage == "data":
                raise DeliveryUncertain("SMTP acknowledgement missing; do not automatically resend") from exc
            if attempt == 2:
                raise
        finally:
            if server is not None:
                server.close()
        time.sleep(2 ** (attempt + 1))


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def send_group(payload: dict, settings: MailSettings, state_path: Path, force: bool = False) -> dict:
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    # A broken ledger must fail closed rather than silently re-send to the whole group.
    if not isinstance(state, dict):
        raise ValueError("Invalid email delivery ledger")
    now = datetime.now(BEIJING)
    generated = datetime.fromisoformat(payload["generated_at"].replace("Z", "+00:00"))
    if generated.tzinfo is None or (now - generated).total_seconds() > 36 * 3600:
        raise ValueError("Refusing to email an outdated or undated dataset")
    day = now.strftime("%Y-%m-%d")
    subject, text, html = build_digest(payload, settings)
    report = {"status": "sent", "accepted": 0, "skipped": 0, "failed": 0, "uncertain": 0, "recipients": len(settings.recipients)}
    for index, recipient in enumerate(settings.recipients):
        identity = hashlib.sha256(f"{recipient.lower()}|{settings.site_url}".encode()).hexdigest()
        prior = state.get(identity, {})
        if not force and prior.get("day") == day:
            if prior.get("status") == "uncertain":
                report["uncertain"] += 1
            else:
                report["skipped"] += 1
            continue
        try:
            deliver(build_message(subject, text, html, settings, recipient, day), settings)
            state[identity] = {"day": day, "status": "accepted"}
            report["accepted"] += 1
        except DeliveryUncertain:
            state[identity] = {"day": day, "status": "uncertain"}
            report["uncertain"] += 1
        except smtplib.SMTPAuthenticationError:
            report["failed"] += len(settings.recipients) - index
            print("[ERROR] SMTP authentication failed; check SMTP service and authorization code")
            break
        except (OSError, smtplib.SMTPException):
            report["failed"] += 1
            print(f"[WARN] Recipient {index + 1} rejected or unavailable; address omitted from logs")
        write_json(state_path, state)
        if index + 1 < len(settings.recipients):
            time.sleep(settings.interval)
    report["status"] = "partial-failure" if report["failed"] or report["uncertain"] else "sent"
    return report


def report_status(path: Path, report: dict) -> None:
    write_json(path, report)
    print(json.dumps(report, ensure_ascii=False))
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write("\n## Email Digest\n\n```json\n" + json.dumps(report, ensure_ascii=False, indent=2) + "\n```\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a private per-recipient paper digest over TLS SMTP")
    parser.add_argument("--config", default=str(ROOT / "config/arxiv.json"))
    parser.add_argument("--data", default=str(ROOT / "site/data/latest.json"))
    parser.add_argument("--state", default=str(ROOT / "data/.email_delivery.json"))
    parser.add_argument("--report", default=str(ROOT / "data/email-status.json"))
    parser.add_argument("--preview-dir", default=str(ROOT / "data/email-preview"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if not args.dry_run and not args.check_config and os.environ.get("EMAIL_ENABLED", "false").lower() != "true":
        report_status(Path(args.report), {"status": "disabled", "action": "Set repository variable EMAIL_ENABLED=true and SMTP Secrets"})
        return 0
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        settings = settings_from_env(config, preview=args.dry_run)
        if args.check_config:
            report_status(Path(args.report), {"status": "configuration-ready", "recipients": len(settings.recipients), "security": settings.security})
            return 0
        payload = json.loads(Path(args.data).read_text(encoding="utf-8"))
        if args.dry_run:
            _, text, html = build_digest(payload, settings)
            directory = Path(args.preview_dir)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "digest.html").write_text(html, encoding="utf-8")
            (directory / "digest.txt").write_text(text, encoding="utf-8")
            report = {"status": "dry-run", "recipients": len(settings.recipients), "network_calls": 0}
        else:
            report = send_group(payload, settings, Path(args.state), args.force)
        report_status(Path(args.report), report)
        return 1 if report.get("failed") or report.get("uncertain") else 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Configuration errors are deliberately phrased without credentials or addresses.
        detail = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else type(exc).__name__
        report_status(Path(args.report), {"status": "failed", "detail": detail})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
