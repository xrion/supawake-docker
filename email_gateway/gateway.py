#!/usr/bin/env python3
"""Email -> OpenHands conversation gateway.

Polls a mailbox over IMAP, turns each allowed message into a new Agent Canvas
conversation, and (optionally) replies with the conversation link.

Commands
--------
poll                 Process unseen emails once.
watch                Poll forever (interval from config).
parse FILE           Show routing/parsing for a .eml file (no side effects).
send ...             Send an email through the configured SMTP account.
selftest             Run parsing/routing assertions.

Security: nothing is processed unless the sender is in
``security.allowed_senders`` (or the subject carries the configured prefix).
"""

from __future__ import annotations

import argparse
import email
import email.policy
import html
import imaplib
import json
import logging
import os
import re
import smtplib
import ssl
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
STATE_FILE = HERE / ".state.json"
LOG = logging.getLogger("email-gateway")
AUTOMATION_STATE_KEY = "email-gateway-state"


def automation_secret(name: str) -> str | None:
    """Read a secret from the agent server when running as an automation."""
    base = os.environ.get("AGENT_SERVER_URL", "").rstrip("/")
    key = os.environ.get("SESSION_API_KEY") or os.environ.get("OH_SESSION_API_KEYS_0", "")
    if not key:
        path = Path.home() / ".openhands" / "agent-canvas" / "api-key.txt"
        if path.exists():
            key = path.read_text(encoding="utf-8").strip()
    if not base or not key:
        return None
    request = Request(f"{base}/api/settings/secrets/{name}", headers={"X-Session-API-Key": key})
    try:
        with urlopen(request, timeout=15) as response:
            return response.read().decode().strip() or None
    except Exception as exc:
        LOG.warning("Could not read secret %s: %s", name, exc)
        return None


def _kv_env() -> tuple[str, str]:
    return (
        os.environ.get("AUTOMATION_API_URL", "").rstrip("/"),
        os.environ.get("AUTOMATION_KV_TOKEN", ""),
    )


def kv_get(key: str) -> Any:
    base, token = _kv_env()
    if not base or not token:
        return None
    request = Request(f"{base}/v1/kv/{key}", headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(request, timeout=15) as response:
            body = json.loads(response.read() or b"{}")
        return body.get("value")
    except Exception as exc:
        LOG.warning("KV get %s failed: %s", key, exc)
        return None


def kv_set(key: str, value: Any) -> None:
    base, token = _kv_env()
    if not base or not token:
        return
    request = Request(
        f"{base}/v1/kv/{key}",
        data=json.dumps(value).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="PUT",
    )
    try:
        with urlopen(request, timeout=30) as response:
            response.read()
    except Exception as exc:
        LOG.warning("KV set %s failed: %s", key, exc)

DIRECTIVE_PATTERNS = {
    "project": r"^\s*(?:projet|project|proyecto)\s*[:\-]\s*(.+?)\s*$",
    "workspace": r"^\s*(?:workspace|espace\s+de\s+travail)\s*[:\-]\s*(.+?)\s*$",
    "repo": r"^\s*(?:repo|d[ée]p[ôo]t|repository)\s*[:\-]\s*(.+?)\s*$",
    "branch": r"^\s*(?:branche|branch)\s*[:\-]\s*(.+?)\s*$",
}
SUBJECT_TAG = re.compile(r"\[(projet|project|workspace|repo|branche|branch)\s*:\s*([^\]]+)\]", re.I)


@dataclass
class InboundEmail:
    message_id: str
    sender: str
    recipient: str
    subject: str
    date: str
    body: str
    raw: bytes = b""
    attachments: list[str] = field(default_factory=list)
    gateway_marker: bool = False
    in_reply_to: str = ""
    references: str = ""

    def directives(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for match in SUBJECT_TAG.finditer(self.subject):
            key = match.group(1).lower()
            key = {"projet": "project", "branche": "branch"}.get(key, key)
            found[key] = match.group(2).strip()

        for line in self.body.splitlines():
            for key, pattern in DIRECTIVE_PATTERNS.items():
                if key in found:
                    continue
                match = re.match(pattern, line, re.I)
                if match:
                    found[key] = match.group(1).strip().strip("«»\"'")
        return found


def load_config(path: Path | str = DEFAULT_CONFIG) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"Missing config file: {path} (copy config.example.json)")
    config = json.loads(path.read_text(encoding="utf-8"))

    imap_cfg = config.setdefault("imap", {})
    smtp_cfg = config.setdefault("smtp", {})
    imap_cfg.setdefault("host", "imap.gmail.com")
    imap_cfg.setdefault("port", 993)
    smtp_cfg.setdefault("host", "smtp.gmail.com")
    smtp_cfg.setdefault("port", 587)
    smtp_cfg.setdefault("from", smtp_cfg.get("user", ""))

    imap_cfg["password"] = (
        os.environ.get("EMAIL_IMAP_PASSWORD")
        or os.environ.get("EMAIL_PASSWORD")
        or imap_cfg.get("password", "")
    )
    smtp_cfg["password"] = (
        os.environ.get("EMAIL_SMTP_PASSWORD")
        or os.environ.get("EMAIL_PASSWORD")
        or smtp_cfg.get("password", "")
    )
    if not imap_cfg["password"]:
        secret = automation_secret("EMAIL_PASSWORD")
        imap_cfg["password"] = secret or ""
        smtp_cfg["password"] = secret or ""

    security = config.setdefault("security", {})
    extra_senders = (
        os.environ.get("EMAIL_ALLOWED_SENDERS")
        or automation_secret("EMAIL_ALLOWED_SENDERS")
        or ""
    )
    senders = [str(s).strip() for s in security.get("allowed_senders") or [] if str(s).strip()]
    known = {s.lower() for s in senders}
    for address in re.split(r"[,;\s]+", extra_senders):
        if address and address.lower() not in known:
            senders.append(address)
            known.add(address.lower())
    security["allowed_senders"] = senders
    if not imap_cfg.get("user") or not imap_cfg.get("password"):
        raise SystemExit(
            "Missing IMAP credentials. Set imap.user in config.json and "
            "EMAIL_PASSWORD (or EMAIL_IMAP_PASSWORD) in the environment."
        )
    return config


# --------------------------------------------------------------------------
# Mailbox
# --------------------------------------------------------------------------


def _decode_part(part: email.message.Message) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def parse_message(raw: bytes) -> InboundEmail:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    body_parts: list[str] = []
    attachments: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            disposition = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                if part.get_filename():
                    attachments.append(part.get_filename() or "")
                continue
            if part.get_content_type() == "text/plain":
                body_parts.append(_decode_part(part))
    else:
        if msg.get_content_type() == "text/html":
            body_parts.append(_html_to_text(_decode_part(msg)))
        else:
            body_parts.append(_decode_part(msg))

    body = "\n".join(p for p in body_parts if p).strip()
    if not body:
        body = "(corps vide ou uniquement HTML)"

    return InboundEmail(
        message_id=(msg.get("Message-ID") or "").strip() or f"no-message-id-{time.time_ns()}",
        sender=(msg.get("From") or "").strip(),
        recipient=(msg.get("To") or "").strip(),
        subject=(msg.get("Subject") or "").strip(),
        date=(msg.get("Date") or "").strip(),
        body=body,
        raw=raw,
        attachments=attachments,
        gateway_marker=(msg.get("X-OpenHands-Gateway") or "").strip() == "1",
        in_reply_to=(msg.get("In-Reply-To") or "").strip(),
        references=(msg.get("References") or "").strip(),
    )


def fetch_unseen(config: dict[str, Any]) -> list[tuple[bytes, str]]:
    imap_cfg = config["imap"]
    messages: list[tuple[bytes, str]] = []
    with imaplib.IMAP4_SSL(imap_cfg["host"], int(imap_cfg["port"])) as client:
        client.login(imap_cfg["user"], imap_cfg["password"])
        client.select("INBOX")
        status, data = client.search(None, "UNSEEN")
        if status != "OK" or not data or not data[0]:
            return messages
        for number in data[0].split():
            status, payload = client.fetch(number, "(BODY.PEEK[])")
            if status != "OK" or not payload:
                continue
            for chunk in payload:
                if isinstance(chunk, tuple) and len(chunk) == 2:
                    messages.append((chunk[1], number.decode()))
    return messages


def mark_seen(config: dict[str, Any], uid: str) -> None:
    imap_cfg = config["imap"]
    with imaplib.IMAP4_SSL(imap_cfg["host"], int(imap_cfg["port"])) as client:
        client.login(imap_cfg["user"], imap_cfg["password"])
        client.select("INBOX")
        client.store(uid, "+FLAGS", "\\Seen")


_HTML_WRAPPER = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{title}</title></head>
<body style="margin:0;padding:0;background:#f3f4f6;">
  <div style="max-width:720px;margin:0 auto;padding:24px;background:#ffffff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.6;color:#1f2328;">
    {inner}
    <p style="margin:28px 0 0;padding-top:12px;border-top:1px solid #e5e7eb;font-size:12px;color:#8b949e;">
      Email gateway OpenHands
    </p>
  </div>
</body>
</html>"""

_INLINE_CODE = ('<code style="background:#f6f8fa;border-radius:4px;padding:1px 4px;'
                'font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;">{}</code>')


def _inline_html(text: str) -> str:
    """Inline Markdown on already-escaped text."""
    text = re.sub(r"`([^`\n]+)`", lambda m: _INLINE_CODE.format(m.group(1)), text)
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])__([^_\n]+)__(?![\w_])", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2" style="color:#0969da;">\1</a>', text)
    return text


def _table_html(header: list[str], rows: list[list[str]]) -> str:
    cell = "border:1px solid #d0d7de;padding:6px 10px;vertical-align:top;"
    head = "".join(f'<th style="{cell}background:#f6f8fa;text-align:left;">{_inline_html(html.escape(c))}</th>'
                   for c in header)
    body = "".join(
        "<tr>" + "".join(f'<td style="{cell}">{_inline_html(html.escape(c))}</td>' for c in row) + "</tr>"
        for row in rows
    )
    return (f'<table style="border-collapse:collapse;margin:0 0 14px;width:100%;font-size:14px;">'
            f"<tr>{head}</tr>{body}</table>")


def markdown_to_html(text: str) -> str:
    """Small Markdown renderer producing email-friendly inline-styled HTML."""
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            joined = "<br>".join(_inline_html(html.escape(line)) for line in paragraph)
            out.append(f'<p style="margin:0 0 12px;">{joined}</p>')
            paragraph.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"^\s*```", line):
            flush()
            i += 1
            code: list[str] = []
            while i < len(lines) and not re.match(r"^\s*```", lines[i]):
                code.append(lines[i])
                i += 1
            i += 1
            out.append(
                '<pre style="margin:0 0 14px;padding:12px;background:#f6f8fa;border:1px solid #e5e7eb;'
                'border-radius:6px;overflow-x:auto;font-size:13px;line-height:1.5;"><code>'
                f"{html.escape(chr(10).join(code))}</code></pre>"
            )
            continue

        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            flush()
            level = min(len(heading.group(1)) + 1, 6)
            size = {2: "20px", 3: "17px", 4: "16px", 5: "15px", 6: "14px"}[level]
            out.append(
                f'<h{level} style="margin:18px 0 8px;font-size:{size};line-height:1.3;">'
                f"{_inline_html(html.escape(heading.group(2)))}</h{level}>"
            )
            i += 1
            continue

        if re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", line):
            flush()
            out.append('<hr style="border:0;border-top:1px solid #e5e7eb;margin:20px 0;">')
            i += 1
            continue

        if re.match(r"^\s*[-*+]\s+", line):
            flush()
            items = []
            while i < len(lines) and re.match(r"^\s*[-*+]\s+", lines[i]):
                items.append(_inline_html(html.escape(re.sub(r"^\s*[-*+]\s+", "", lines[i]))))
                i += 1
            out.append('<ul style="margin:0 0 12px;padding-left:22px;">'
                       + "".join(f'<li style="margin:2px 0;">{item}</li>' for item in items) + "</ul>")
            continue

        if re.match(r"^\s*\d+[.)]\s+", line):
            flush()
            items = []
            while i < len(lines) and re.match(r"^\s*\d+[.)]\s+", lines[i]):
                items.append(_inline_html(html.escape(re.sub(r"^\s*\d+[.)]\s+", "", lines[i]))))
                i += 1
            out.append('<ol style="margin:0 0 12px;padding-left:22px;">'
                       + "".join(f'<li style="margin:2px 0;">{item}</li>' for item in items) + "</ol>")
            continue

        if line.lstrip().startswith(">"):
            flush()
            quote: list[str] = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append('<blockquote style="margin:0 0 12px;padding:4px 0 4px 12px;'
                       'border-left:3px solid #d0d7de;color:#57606a;">'
                       + markdown_to_html("\n".join(quote)) + "</blockquote>")
            continue

        if "|" in line and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]+$", lines[i + 1]):
            flush()
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            rows: list[list[str]] = []
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            out.append(_table_html(header, rows))
            continue

        if not line.strip():
            flush()
            i += 1
            continue

        paragraph.append(line)
        i += 1

    flush()
    return "\n".join(out)


def html_email(title: str, inner: str) -> str:
    return _HTML_WRAPPER.format(title=html.escape(title), inner=inner)


def _code(value: str) -> str:
    return _INLINE_CODE.format(html.escape(str(value)))


def _refs_list_html(references: list[dict[str, Any]]) -> str:
    items = []
    for ref in references:
        items.append(
            '<li style="margin:8px 0;">'
            f'<strong>{html.escape(ref["project"])}</strong><br>'
            f'<a href="{html.escape(ref["url"])}" style="color:#0969da;">{html.escape(ref["url"])}</a><br>'
            f'<span style="color:#57606a;font-size:13px;">Conversation {_code(ref["conversation_id"])}'
            f' &middot; reprise {_code("[conv:" + short_id(ref["conversation_id"]) + "]")}</span>'
            "</li>"
        )
    return '<ul style="margin:0 0 16px;padding-left:22px;">' + "".join(items) + "</ul>"


def ack_email_html(mail: InboundEmail, launched: list[dict[str, Any]],
                   inbox_id: str | None = None) -> str:
    inner = (
        '<h2 style="margin:0 0 12px;font-size:20px;">Demande reçue</h2>'
        f'<p style="margin:0 0 12px;">Objet : <strong>{html.escape(mail.subject or "(sans objet)")}</strong></p>'
        f'<p style="margin:0 0 12px;">{len(launched)} conversation(s) OpenHands ont été lancée(s) :</p>'
        + _refs_list_html(launched)
        + (f'<p style="margin:0 0 12px;color:#57606a;">Demande brute archivée : {_code(inbox_id)}</p>'
           if inbox_id else "")
        + "<p style=\"margin:0 0 12px;\">Répondez à cet email en gardant l'identifiant "
        + _code("[conv:...]")
        + " dans l'objet pour reprendre une conversation. Vous recevrez un nouveau message "
          "à la fin de chacune.</p>"
    )
    return html_email("Demande reçue", inner)


def resume_email_html(mail: InboundEmail, item: dict[str, Any],
                      conversation_id: str, url: str) -> str:
    ref = {
        "project": Path(item.get("workspace") or "Conversation").name or "Conversation",
        "url": url,
        "conversation_id": conversation_id,
    }
    inner = (
        '<h2 style="margin:0 0 12px;font-size:20px;">Message ajouté</h2>'
        f'<p style="margin:0 0 12px;">Objet : <strong>{html.escape(mail.subject or "(sans objet)")}</strong></p>'
        '<p style="margin:0 0 12px;">Votre message a été ajouté à la conversation existante.</p>'
        + _refs_list_html([ref])
        + '<p style="margin:0 0 12px;">Répondez à cet email pour poursuivre le même travail.</p>'
    )
    return html_email("Message ajouté", inner)


def finished_email_html(item: dict[str, Any], conversation_id: str, status: str,
                        answer: str, url: str) -> str:
    color = "#1a7f37" if status == "finished" else "#9a6700"
    answer_html = (markdown_to_html(answer) if answer else
                   '<p style="margin:0 0 12px;color:#57606a;">(pas de réponse texte)</p>')
    inner = (
        '<h2 style="margin:0 0 12px;font-size:20px;">Conversation terminée</h2>'
        f'<p style="margin:0 0 12px;">Objet : <strong>{html.escape(item.get("subject") or "(sans objet)")}</strong></p>'
        f'<p style="margin:0 0 12px;">Statut : <strong style="color:{color};">{html.escape(status)}</strong></p>'
        f'<p style="margin:0 0 12px;">Espace de travail : {_code(item.get("workspace", ""))}</p>'
        f'<p style="margin:0 0 12px;">Lien : <a href="{html.escape(url)}" style="color:#0969da;">'
        f"{html.escape(url)}</a></p>"
        f'<p style="margin:0 0 12px;">Conversation {_code(conversation_id)} &middot; reprise '
        f'{_code("[conv:" + short_id(conversation_id) + "]")}</p>'
        '<hr style="border:0;border-top:1px solid #e5e7eb;margin:20px 0;">'
        "<h3 style=\"margin:18px 0 8px;font-size:17px;\">Réponse de l'agent</h3>"
        + answer_html
        + '<hr style="border:0;border-top:1px solid #e5e7eb;margin:20px 0;">'
        + '<p style="margin:0 0 12px;">Pour reprendre ce travail, répondez simplement à cet email '
          "en gardant l'identifiant "
        + _code("[conv:...]")
        + " dans l'objet.</p>"
    )
    return html_email("Conversation terminée", inner)


def build_email_message(config: dict[str, Any], to: str, subject: str, body: str,
                        html_body: str | None = None,
                        in_reply_to: str | None = None) -> EmailMessage:
    smtp_cfg = config["smtp"]
    msg = EmailMessage()
    msg["From"] = smtp_cfg.get("from") or smtp_cfg["user"]
    msg["To"] = to
    msg["Subject"] = subject
    msg["X-OpenHands-Gateway"] = "1"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    return msg


def send_email(config: dict[str, Any], to: str, subject: str, body: str,
               in_reply_to: str | None = None, html_body: str | None = None) -> None:
    smtp_cfg = config["smtp"]
    msg = build_email_message(config, to, subject, body, html_body, in_reply_to)

    with (smtplib.SMTP_SSL(smtp_cfg["host"], int(smtp_cfg["port"]), timeout=30,
                           context=ssl.create_default_context())
          if smtp_cfg.get("secure") else
          smtplib.SMTP(smtp_cfg["host"], int(smtp_cfg["port"]), timeout=30)) as client:
        client.ehlo()
        if not smtp_cfg.get("secure") and smtp_cfg.get("starttls", True):
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        client.login(smtp_cfg["user"], smtp_cfg["password"])
        client.send_message(msg)


# --------------------------------------------------------------------------
# Agent Canvas
# --------------------------------------------------------------------------


def api_key() -> str:
    for name in ("AGENT_CANVAS_API_KEY", "SESSION_API_KEY", "OH_SESSION_API_KEY",
                 "AUTOMATION_AGENT_SERVER_API_KEY"):
        value = os.environ.get(name)
        if value:
            return value.strip()
    path = Path.home() / ".openhands" / "agent-canvas" / "api-key.txt"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    raise SystemExit("No Agent Canvas API key found")


def _request(config: dict[str, Any], method: str, path: str, payload: Any = None,
             headers: dict[str, str] | None = None) -> Any:
    base = (
        os.environ.get("AGENT_SERVER_URL")
        or config.get("gateway", {}).get("api_base")
        or "http://127.0.0.1:80"
    ).rstrip("/")
    all_headers = {"X-Session-API-Key": api_key()}
    if headers:
        all_headers.update(headers)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        all_headers["Content-Type"] = "application/json"
    request = Request(f"{base}{path}", data=data, method=method, headers=all_headers)
    try:
        with urlopen(request, timeout=60) as response:
            body = response.read()
            return json.loads(body) if body else None
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:1000]
        raise RuntimeError(f"{method} {path} -> {exc.code}: {detail}") from exc


def list_workspaces(config: dict[str, Any]) -> list[dict[str, Any]]:
    return _request(config, "GET", "/api/workspaces").get("workspaces", [])


def register_workspace(config: dict[str, Any], path: Path, name: str) -> None:
    parent = str(path.parent)
    _request(config, "POST", "/api/workspaces", {
        "workspaces": [{"id": str(path), "name": name, "path": str(path), "parentPath": parent}]
    })


def slugify(value: str) -> str:
    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE).strip()
    value = re.sub(r"[\s_]+", "-", value)
    return value or "projet"


def _routing(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("routing", {})


def _default_workspace(config: dict[str, Any]) -> Path:
    routing = _routing(config)
    projects_root = Path(routing.get("projects_root", "/home/openhands/workspace/projects")).expanduser()
    return Path(routing.get("default_workspace", projects_root / "general")).expanduser()


def _workspace_name(config: dict[str, Any], path: Path) -> str:
    for workspace in list_workspaces(config):
        if Path(workspace.get("path", "")).expanduser() == path.expanduser():
            return workspace.get("name", path.name)
    return path.name


def resolve_workspace(config: dict[str, Any], directives: dict[str, str]) -> tuple[Path, str]:
    routing = _routing(config)
    projects_root = Path(routing.get("projects_root", "/home/openhands/workspace/projects")).expanduser()
    default_ws = _default_workspace(config)
    create_missing = routing.get("create_missing_workspaces", True)

    explicit = directives.get("workspace")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = projects_root / explicit
        if not path.exists() and create_missing:
            path.mkdir(parents=True, exist_ok=True)
        return path, _workspace_name(config, path)

    project = directives.get("project")
    if project:
        alias = (routing.get("workspace_aliases") or {}).get(project.lower())
        if alias:
            path = Path(alias).expanduser()
            return path, _workspace_name(config, path)
        for workspace in list_workspaces(config):
            if workspace.get("name", "").lower() == project.lower() or \
               Path(workspace.get("path", "")).name.lower() == project.lower():
                return Path(workspace["path"]), workspace["name"]
        path = projects_root / slugify(project)
        if not path.exists() and create_missing:
            path.mkdir(parents=True, exist_ok=True)
            register_workspace(config, path, project)
        return path, project

    return default_ws, _workspace_name(config, default_ws)


def normalize_subject(subject: str) -> str:
    value = re.sub(r"\[(?:conv|conversation)\s*:[^\]]*\]", " ", subject, flags=re.I)
    value = re.sub(r"^(\s*(?:re|fwd?|aw|tr|rép)\s*:\s*)+", "", value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip().lower()


def conversation_ref(mail: InboundEmail) -> str | None:
    for text in (mail.subject, mail.body):
        match = re.search(r"\[(?:conv|conversation)\s*:\s*([0-9a-f-]{8,})\]", text, re.I)
        if match:
            return match.group(1).lower()
        match = re.search(
            r"(?:conversation(?:\s+id)?|conv(?:ersation)?)\s*[:\-]\s*([0-9a-f]{8}-[0-9a-f-]{27,})",
            text, re.I,
        )
        if match:
            return match.group(1).lower()
    return None


def match_workspaces(config: dict[str, Any], text: str) -> list[tuple[Path, str]]:
    """Return workspaces explicitly named in the email, in order of mention."""
    routing = _routing(config)
    default_ws = _default_workspace(config).expanduser()
    ignored = {default_ws, Path(routing.get("inbox_workspace", "")).expanduser()}
    candidates: list[tuple[str, Path, str]] = []

    for workspace in list_workspaces(config):
        path = Path(workspace.get("path", "")).expanduser()
        if path in ignored:
            continue
        name = workspace.get("name") or path.name
        for term in {name, path.name}:
            if len(term) >= 4:
                candidates.append((term, path, name))
    for term, raw_path in (routing.get("workspace_aliases") or {}).items():
        path = Path(raw_path).expanduser()
        if Path(raw_path).expanduser() in ignored:
            continue
        candidates.append((term, path, _workspace_name(config, path)))

    found: list[tuple[int, Path, str]] = []
    seen: set[str] = set()
    lowered = text.lower()
    for term, path, name in candidates:
        match = re.search(rf"(?<![\w-]){re.escape(term.lower())}(?![\w-])", lowered)
        if not match or str(path) in seen:
            continue
        seen.add(str(path))
        found.append((match.start(), path, name))
    return [(path, name) for _, path, name in sorted(found)]


def resolve_targets(config: dict[str, Any], mail: InboundEmail) -> list[tuple[Path, str]]:
    """Workspaces that should receive a working conversation for this email."""
    directives = mail.directives()
    routing = _routing(config)
    if directives.get("workspace") or directives.get("project"):
        return [resolve_workspace(config, directives)]

    text = f"{mail.subject}\n{mail.body}".lower()
    general_keywords = [k.lower() for k in routing.get("general_keywords", [])]
    if any(keyword in text for keyword in general_keywords):
        default_ws = _default_workspace(config)
        return [(default_ws, _workspace_name(config, default_ws))]

    matched = match_workspaces(config, text)
    if matched:
        return matched
    default_ws = _default_workspace(config)
    return [(default_ws, _workspace_name(config, default_ws))]


def build_prompt(mail: InboundEmail, directives: dict[str, str], workspace: Path,
                 workspace_name: str | None = None, focus: str | None = None,
                 inbox_conversation_id: str | None = None) -> str:
    lines = [
        "[Demande reçue par email]",
        f"De : {mail.sender}",
        f"À : {mail.recipient}",
        f"Date : {mail.date}",
        f"Objet : {mail.subject}",
    ]
    if mail.attachments:
        lines.append("Pièces jointes : " + ", ".join(mail.attachments))
    if directives:
        lines.append("Consignes détectées : " + ", ".join(f"{k}={v}" for k, v in directives.items()))
    if inbox_conversation_id:
        lines.append(f"Demande brute archivée : {inbox_conversation_id}")
    lines += [
        "",
        "--- Corps du message ---",
        mail.body.strip()[: int(os.environ.get("EMAIL_MAX_BODY_CHARS", "30000"))],
        "--- Fin du message ---",
        "",
        f"Tu travailles dans l'espace « {workspace_name or workspace.name} » ({workspace}).",
    ]
    if focus:
        lines.append(
            f"Cet email couvre plusieurs sujets. Traite uniquement la partie concernant "
            f"« {focus} » et ignore les demandes qui relèvent des autres espaces."
        )
    lines += [
        "Traite la demande ci-dessus de bout en bout avec tes outils.",
        "Si le dépôt ou les fichiers nécessaires ne sont pas présents, récupère-les d'abord.",
        "Si une information essentielle manque, fais une hypothèse raisonnable, indique-la, puis continue.",
        "À la fin, résume ce que tu as fait, les fichiers modifiés et les points à valider.",
    ]
    return "\n".join(lines)


def create_conversation(config: dict[str, Any], mail: InboundEmail, prompt: str,
                        workspace: Path, title: str, run: bool = True,
                        origin: str = "email") -> dict[str, Any]:
    settings = _request(config, "GET", "/api/settings", headers={"X-Expose-Secrets": "encrypted"})
    agent_settings = dict(settings.get("agent_settings") or {})
    agent_settings.pop("schema_version", None)
    agent_settings.pop("mcp_config", None)

    tools = {t.get("name"): t for t in agent_settings.get("tools") or []}
    for name, params in {
        "terminal": {},
        "file_editor": {},
        "task_tracker": {},
        "browser_tool_set": {},
        "canvas_ui": {},
    }.items():
        tools.setdefault(name, {"name": name, "params": params})
    agent_settings["tools"] = list(tools.values())

    context = dict(agent_settings.get("agent_context") or {})
    context.update({"load_public_skills": True, "load_user_skills": True, "load_project_skills": True})
    agent_settings["agent_context"] = context

    max_iterations = int(config.get("agent", {}).get("max_iterations", 500))
    payload = {
        "secrets_encrypted": True,
        "agent_settings": agent_settings,
        "tool_module_qualnames": {"canvas_ui": "canvas_ui_tool"},
        "workspace": {"kind": "LocalWorkspace", "working_dir": str(workspace)},
        "confirmation_policy": {"kind": "NeverConfirm"},
        "max_iterations": max_iterations,
        "stuck_detection": True,
        "autotitle": False,
        "worktree": False,
        "tags": {
            "clientsource": "agentcanvas",
            "workspace": str(workspace),
            "origin": origin,
        },
        "initial_message": {
            "role": "user",
            "content": [{"type": "text", "text": prompt}],
            "run": run,
        },
    }
    created = _request(config, "POST", "/api/conversations", payload)
    if title:
        try:
            _request(config, "PATCH", f"/api/conversations/{created['id']}", {"title": title[:200]})
            created["title"] = title[:200]
        except Exception as exc:  # title is cosmetic; never fail the launch for it
            LOG.warning("Could not set title: %s", exc)
    return created


def append_message(config: dict[str, Any], conversation_id: str, text: str,
                   run: bool = True) -> None:
    _request(config, "POST", f"/api/conversations/{conversation_id}/events", {
        "role": "user",
        "content": [{"type": "text", "text": text}],
        "run": run,
    })


def inbox_workspace(config: dict[str, Any]) -> Path:
    routing = _routing(config)
    projects_root = Path(routing.get("projects_root", "/home/openhands/workspace/projects")).expanduser()
    path = Path(routing.get("inbox_workspace", projects_root / "email")).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def archive_email(config: dict[str, Any], mail: InboundEmail) -> str | None:
    """Keep the untouched raw request in the Email workspace.

    The archive conversation never runs: it is a mailbox record, not work.
    """
    if not _routing(config).get("archive_inbox_conversations", True):
        return None
    workspace = inbox_workspace(config)
    safe_id = re.sub(r"[^\w.-]+", "-", mail.message_id.strip("<>"))[:120] or "email"
    dated = workspace / "inbox" / datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dated.mkdir(parents=True, exist_ok=True)
    (dated / f"{safe_id}.eml").write_bytes(mail.raw)

    try:
        created = create_conversation(
            config, mail, build_prompt(mail, mail.directives(), workspace, "Email"),
            workspace, f"📥 {mail.subject}" if mail.subject else f"📥 Email de {mail.sender}",
            run=False, origin="email-inbox",
        )
        return created["id"]
    except Exception as exc:
        LOG.warning("Could not create inbox conversation (raw .eml kept): %s", exc)
        return None


def public_conversation_url(config: dict[str, Any], conversation_id: str) -> str:
    base = (
        config.get("gateway", {}).get("public_base_url")
        or os.environ.get("OPENHANDS_URL")
        or "http://localhost:8000"
    ).rstrip("/")
    return f"{base}/conversations/{conversation_id}"


# --------------------------------------------------------------------------
# Processing
# --------------------------------------------------------------------------


def load_state() -> dict[str, Any]:
    state: dict[str, Any] = {}
    stored = kv_get(AUTOMATION_STATE_KEY)
    if isinstance(stored, dict):
        state = stored
    elif STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    state.setdefault("processed", {})
    state.setdefault("conversations", {})
    state.setdefault("messages", {})
    state.setdefault("threads", {})
    state.setdefault("inbox", {})
    for message_id, item in state["processed"].items():
        conversation_id = item.get("conversation_id")
        if conversation_id:
            state["messages"].setdefault(message_id, conversation_id)
            state["conversations"].setdefault(conversation_id, {
                "message_id": message_id,
                "sender": item.get("sender", ""),
                "subject": item.get("subject", ""),
                "workspace": item.get("workspace", ""),
                "thread": normalize_subject(item.get("subject", "")),
                "notified": item.get("notified", False),
            })
    return state


def save_state(state: dict[str, Any]) -> None:
    processed = state.get("processed", {})
    if len(processed) > 500:
        for key in list(processed)[: len(processed) - 500]:
            processed.pop(key, None)
    for key in ("conversations", "messages", "threads", "inbox"):
        items = state.get(key, {})
        if len(items) > 500:
            for old_key in list(items)[: len(items) - 500]:
                items.pop(old_key, None)
    kv_set(AUTOMATION_STATE_KEY, state)
    try:
        STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")
    except OSError as exc:
        LOG.debug("Could not write local state file: %s", exc)


def is_allowed(config: dict[str, Any], mail: InboundEmail) -> bool:
    security = config.get("security", {})
    senders = [s.lower() for s in security.get("allowed_senders", [])]
    domains = [d.lower().lstrip("@") for d in security.get("allowed_domains", [])]
    prefix = (security.get("subject_prefix") or "").strip()

    address_match = re.search(r"<([^>]+)>", mail.sender)
    address = (address_match.group(1) if address_match else mail.sender).strip().lower()
    domain = address.rsplit("@", 1)[-1] if "@" in address else ""

    # Loop protection: never act on our own replies.
    if mail.gateway_marker:
        return False
    if security.get("ignore_self", True) and address == config.get("imap", {}).get("user", "").lower():
        return False

    if security.get("allow_any_sender", False):
        return True
    if senders and address in senders:
        return True
    if domains and domain in domains:
        return True
    if prefix and mail.subject.strip().lower().startswith(prefix.lower()):
        return True
    return False


def short_id(conversation_id: str) -> str:
    return conversation_id.replace("-", "")[:8]


def _resolve_reference(state: dict[str, Any], reference: str) -> str | None:
    known = set(state["conversations"]) | {
        item.get("conversation_id") for item in state["processed"].values() if item.get("conversation_id")
    }
    if reference in known:
        return reference
    matches = sorted(cid for cid in known if cid.replace("-", "").lower().startswith(reference.replace("-", "").lower()))
    return matches[0] if len(matches) == 1 else None


def _thread_is_recent(thread: dict[str, Any], days: int = 30) -> bool:
    try:
        updated = datetime.fromisoformat(thread.get("updated_at", ""))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    return (datetime.now(timezone.utc) - updated).days <= days


def find_resume_target(state: dict[str, Any], mail: InboundEmail) -> tuple[str, dict[str, Any]] | None:
    reference = conversation_ref(mail)
    if reference:
        conversation_id = _resolve_reference(state, reference)
        if conversation_id:
            return conversation_id, state["conversations"].get(conversation_id, {})

    for header in (mail.in_reply_to, mail.references):
        for message_id in re.findall(r"<[^>]+>", header):
            conversation_id = state["messages"].get(message_id)
            if conversation_id:
                return conversation_id, state["conversations"].get(conversation_id, {})

    thread_key = normalize_subject(mail.subject)
    thread = state["threads"].get(thread_key) if thread_key else None
    if thread and thread.get("conversation_id") and _thread_is_recent(thread):
        conversation_id = thread["conversation_id"]
        return conversation_id, state["conversations"].get(conversation_id, {})
    return None


def build_followup_prompt(mail: InboundEmail, inbox_conversation_id: str | None) -> str:
    lines = [
        "[Suite d'une conversation existante — nouvelle demande reçue par email]",
        f"De : {mail.sender}",
        f"Date : {mail.date}",
        f"Objet : {mail.subject}",
    ]
    if inbox_conversation_id:
        lines.append(f"Demande brute archivée : {inbox_conversation_id}")
    lines += [
        "",
        "--- Nouveau message ---",
        mail.body.strip()[: int(os.environ.get("EMAIL_MAX_BODY_CHARS", "30000"))],
        "--- Fin du message ---",
        "",
        "Reprends le travail déjà commencé dans cette conversation, en t'appuyant sur son historique.",
        "Traite la nouvelle demande de bout en bout, puis résume ce qui a changé.",
    ]
    return "\n".join(lines)


def process_email(config: dict[str, Any], mail: InboundEmail, dry_run: bool = False) -> dict[str, Any]:
    state = load_state()
    if mail.message_id in state["processed"]:
        return {"status": "duplicate", "message_id": mail.message_id}

    if not is_allowed(config, mail):
        LOG.warning("Ignored email from %s: sender not allowed", mail.sender)
        return {"status": "ignored", "message_id": mail.message_id}

    directives = mail.directives()
    resume = find_resume_target(state, mail)
    targets = resolve_targets(config, mail)
    title = f"📧 {mail.subject}" if mail.subject else f"📧 Email de {mail.sender}"
    thread = normalize_subject(mail.subject)

    if dry_run:
        return {
            "status": "dry-run",
            "message_id": mail.message_id,
            "resume_conversation_id": resume[0] if resume else None,
            "targets": [{"workspace": str(path), "project": name} for path, name in targets],
            "directives": directives,
            "title": title,
        }

    inbox_id = archive_email(config, mail)

    if resume:
        conversation_id, item = resume
        append_message(config, conversation_id, build_followup_prompt(mail, inbox_id))
        entry = state["conversations"].setdefault(conversation_id, {})
        entry.update({
            "message_id": mail.message_id,
            "sender": mail.sender,
            "subject": mail.subject,
            "thread": thread,
            "notified": False,
        })
        item = entry
        state["messages"][mail.message_id] = conversation_id
        state["processed"][mail.message_id] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "conversation_id": conversation_id,
            "workspace": item.get("workspace", ""),
            "sender": mail.sender,
            "subject": mail.subject,
            "thread": thread,
            "resumed": True,
            "inbox_conversation_id": inbox_id,
        }
        if thread:
            state["threads"][thread] = {
                "conversation_id": conversation_id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        save_state(state)
        url = public_conversation_url(config, conversation_id)
        if config.get("gateway", {}).get("reply", True):
            try:
                send_email(
                    config, to=mail.sender,
                    subject=f"Re: [conv:{short_id(conversation_id)}] {mail.subject}",
                    body=(
                        "Bonjour,\n\n"
                        "Votre message a été ajouté à la conversation existante.\n\n"
                        f"Espace de travail : {item.get('workspace', '')}\n"
                        f"Conversation ID : {conversation_id}\n"
                        f"Lien : {url}\n\n"
                        "Répondez à cet email pour poursuivre le même travail.\n"
                    ),
                    in_reply_to=mail.message_id,
                )
            except Exception as exc:
                LOG.error("Reply failed: %s", exc)
        return {"status": "resumed", "conversation_id": conversation_id, "url": url,
                "inbox_conversation_id": inbox_id}

    launched: list[dict[str, Any]] = []
    multi = len(targets) > 1
    for workspace, project_name in targets:
        prompt = build_prompt(
            mail, directives, workspace, project_name,
            focus=project_name if multi else None,
            inbox_conversation_id=inbox_id,
        )
        conv_title = f"📧 [{project_name}] {mail.subject}" if multi else title
        created = create_conversation(config, mail, prompt, workspace, conv_title)
        launched.append({
            "conversation_id": created["id"],
            "workspace": str(workspace),
            "project": project_name,
            "url": public_conversation_url(config, created["id"]),
        })
        state["conversations"][created["id"]] = {
            "message_id": mail.message_id,
            "sender": mail.sender,
            "subject": mail.subject,
            "workspace": str(workspace),
            "thread": thread,
            "notified": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        LOG.info("Conversation %s created in %s", created["id"], workspace)

    primary = launched[0]["conversation_id"]
    state["messages"][mail.message_id] = primary
    state["processed"][mail.message_id] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "conversation_id": primary,
        "conversation_ids": [c["conversation_id"] for c in launched],
        "workspace": launched[0]["workspace"],
        "sender": mail.sender,
        "subject": mail.subject,
        "thread": thread,
        "inbox_conversation_id": inbox_id,
    }
    if thread:
        state["threads"][thread] = {
            "conversation_id": primary,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    if inbox_id:
        state["inbox"][inbox_id] = {"message_id": mail.message_id, "subject": mail.subject}
    save_state(state)

    if config.get("gateway", {}).get("reply", True):
        details = "\n".join(
            f"- {c['project']} : {c['url']}\n  Conversation ID : {c['conversation_id']}"
            f"\n  Reprise : [conv:{short_id(c['conversation_id'])}]"
            for c in launched
        )
        try:
            send_email(
                config,
                to=mail.sender,
                subject=f"Re: {mail.subject}",
                body=(
                    "Bonjour,\n\n"
                    f"{len(launched)} conversation(s) OpenHands ont été lancée(s).\n\n"
                    f"{details}\n\n"
                    + (f"Demande brute archivée : {inbox_id}\n\n" if inbox_id else "")
                    + "Pour reprendre une conversation, répondez à cet email en gardant "
                    "son identifiant [conv:...] dans l'objet.\n"
                    "Vous recevrez un nouveau message à la fin de chaque conversation.\n"
                ),
                in_reply_to=mail.message_id,
            )
        except Exception as exc:
            LOG.error("Reply failed: %s", exc)

    return {
        "status": "launched",
        "conversations": launched,
        "conversation_id": primary,
        "inbox_conversation_id": inbox_id,
        "directives": directives,
    }


def poll(config: dict[str, Any], dry_run: bool = False, limit: int | None = None) -> list[dict[str, Any]]:
    results = []
    for raw, uid in fetch_unseen(config):
        if limit is not None and len(results) >= limit:
            break
        mail = parse_message(raw)
        try:
            result = process_email(config, mail, dry_run=dry_run)
        except Exception as exc:
            LOG.exception("Failed to process %s: %s", mail.message_id, exc)
            result = {"status": "error", "message_id": mail.message_id, "error": str(exc)}
        results.append(result)
        if result.get("status") in ("launched", "resumed", "ignored", "duplicate") and not dry_run:
            try:
                mark_seen(config, uid)
            except Exception as exc:
                LOG.warning("Could not mark %s as seen: %s", uid, exc)
    return results


def notify_finished(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Email the final answer of every tracked conversation that has finished."""
    state = load_state()
    results: list[dict[str, Any]] = []
    for conversation_id, item in state["conversations"].items():
        if item.get("notified"):
            continue
        try:
            info = _request(config, "GET", f"/api/conversations/{conversation_id}")
        except Exception as exc:
            LOG.warning("Cannot fetch %s: %s", conversation_id, exc)
            continue
        status = (info or {}).get("execution_status")
        if status not in ("finished", "error", "stuck", "stopped"):
            continue

        answer = ""
        if status == "finished":
            try:
                answer = (_request(config, "GET", f"/api/conversations/{conversation_id}/agent_final_response") or {}).get("response", "")
            except Exception as exc:
                LOG.warning("No final response for %s: %s", conversation_id, exc)

        body = (
            f"La conversation « {item.get('subject', '')} » est terminée ({status}).\n\n"
            f"Espace de travail : {item.get('workspace', '')}\n"
            f"Conversation ID : {conversation_id}\n"
            f"Lien : {public_conversation_url(config, conversation_id)}\n\n"
            "--- Réponse de l'agent ---\n"
            f"{answer or '(pas de réponse texte)'}\n\n"
            "--- Pour reprendre ce travail ---\n"
            "Répondez simplement à cet email : la réponse sera ajoutée à la même conversation.\n"
            f"Identifiant à conserver dans l'objet : [conv:{short_id(conversation_id)}]\n"
        )
        try:
            send_email(
                config, to=item.get("sender", ""),
                subject=f"Re: [conv:{short_id(conversation_id)}] {item.get('subject', '')}",
                body=body,
                in_reply_to=item.get("message_id"),
            )
            item["notified"] = True
            results.append({"status": "notified", "conversation_id": conversation_id})
        except Exception as exc:
            LOG.error("Notification failed for %s: %s", conversation_id, exc)
            results.append({"status": "notify-error", "conversation_id": conversation_id, "error": str(exc)})
    save_state(state)
    return results


def send_startup_note(config: dict[str, Any]) -> bool:
    """Send a short, zero-LLM restart checklist (throttled)."""
    gateway_cfg = config.get("gateway", {})
    if not gateway_cfg.get("startup_note", False):
        return False
    recipients = gateway_cfg.get("startup_note_to") or (config.get("security", {}).get("allowed_senders") or [])
    if not recipients:
        return False

    state = load_state()
    min_hours = float(gateway_cfg.get("startup_note_min_hours", 6))
    last = state.get("startup_note_at")
    if last:
        try:
            previous = datetime.fromisoformat(last)
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - previous).total_seconds() < min_hours * 3600:
                return False
        except (ValueError, TypeError):
            pass

    routing = _routing(config)
    workspace_count = "?"
    try:
        workspace_count = str(len(list_workspaces(config)))
    except Exception as exc:
        LOG.warning("Startup note: workspace count unavailable: %s", exc)
    body = (
        "La passerelle email a redémarré.\n\n"
        "Vérifications rapides (aucune conversation/token nécessaire) :\n"
        "1. Espaces attendus : Général, Email, Supawake + projets\n"
        "2. Le menu \"Nouveau chat\" ne propose plus \"No workspace\" ; "
        "un chat sans workspace part dans Général.\n"
        "3. Commande: python3 email_gateway/gateway.py selftest\n"
        "4. Commande: python3 email_gateway/gateway.py poll --dry-run --limit 1\n"
        "5. Statut: pgrep -af \"gateway.py watch\"\n"
        "6. Détail: ./scripts/healthcheck.sh\n\n"
        f"État actuel : intervalle={gateway_cfg.get('poll_interval_seconds', 300)}s, "
        f"défaut={routing.get('default_workspace', '')}, "
        f"archive={routing.get('inbox_workspace', '')}, "
        f"espaces={workspace_count}.\n\n"
        "Répondre à cet email crée/continue une conversation. "
        "Les réponses de fin contiennent [conv:xxxxxxxx].\n"
    )
    try:
        send_email(config, recipients[0],
                   "[OpenHands] Passerelle relancée — vérifier que tout fonctionne", body)
    except Exception as exc:
        LOG.error("Startup note failed: %s", exc)
        return False

    state["startup_note_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    LOG.info("Startup note sent to %s", recipients[0])
    return True



# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def command_selftest() -> int:
    sample = InboundEmail(
        message_id="<test@example.com>",
        sender="Jean <jean@example.com>",
        recipient="agent@example.com",
        subject="[projet:Intel] Analyse du rapport",
        date="Mon, 3 Nov 2026 10:00:00 +0100",
        body="Bonjour,\n\nPeux-tu analyser ce rapport ?\n\nProject: Intel\nRepo: xrion/intel-app\nBranch: main\n",
    )
    directives = sample.directives()
    assert directives["project"] == "Intel", directives
    assert directives["repo"] == "xrion/intel-app", directives
    assert directives["branch"] == "main", directives

    other = InboundEmail(
        message_id="<test2@example.com>",
        sender="a@b.c", recipient="x@y.z", subject="Travail",
        date="", body="Projet : PBDR Homes\nWorkspace : /tmp/demo\n",
    )
    assert other.directives()["project"] == "PBDR Homes", other.directives()
    assert other.directives()["workspace"] == "/tmp/demo", other.directives()

    cfg = {"security": {"allowed_senders": ["jean@example.com"]}}
    assert is_allowed(cfg, sample)
    assert not is_allowed(cfg, other)
    cfg2 = {"security": {"allowed_senders": [], "subject_prefix": "[OH]"}}
    ok = InboundEmail("id", "x@y", "z", "[OH] Fais ceci", "", "corps")
    assert is_allowed(cfg2, ok)
    assert not is_allowed(cfg2, other)

    assert slugify("PBDR Homes") == "PBDR-Homes"

    reply = InboundEmail(
        message_id="<reply@example.com>", sender="jean@example.com", recipient="agent@example.com",
        subject="Re: [conv:abcd1234] Analyse du rapport", date="", body="Continue avec le point 2.",
        in_reply_to="<answer@example.com>", references="<answer@example.com>",
    )
    assert conversation_ref(reply) == "abcd1234", conversation_ref(reply)
    assert normalize_subject(reply.subject) == "analyse du rapport", normalize_subject(reply.subject)

    state = load_state()
    state["conversations"]["abcd1234-0000-0000-0000-000000000000"] = {
        "workspace": "/tmp/demo", "subject": "Analyse du rapport", "notified": False,
    }
    state["messages"]["<answer@example.com>"] = "abcd1234-0000-0000-0000-000000000000"
    resumed = find_resume_target(state, reply)
    assert resumed and resumed[0] == "abcd1234-0000-0000-0000-000000000000", resumed
    print("selftest OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_poll = sub.add_parser("poll", help="process unseen emails once")
    p_poll.add_argument("--dry-run", action="store_true")
    p_poll.add_argument("--limit", type=int)

    p_watch = sub.add_parser("watch", help="process unseen emails forever")
    p_watch.add_argument("--dry-run", action="store_true")

    sub.add_parser("notify", help="email results of finished conversations")

    sub.add_parser("run", help="one poll cycle plus result notifications")

    p_parse = sub.add_parser("parse", help="parse a .eml file without side effects")
    p_parse.add_argument("file")

    p_send = sub.add_parser("send", help="send an email")
    p_send.add_argument("--to", required=True)
    p_send.add_argument("--subject", required=True)
    p_send.add_argument("--body", required=True)

    sub.add_parser("selftest", help="run parsing assertions")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.command == "selftest":
        return command_selftest()

    if args.command == "parse":
        mail = parse_message(Path(args.file).read_bytes())
        print(json.dumps({
            "message_id": mail.message_id,
            "sender": mail.sender,
            "subject": mail.subject,
            "directives": mail.directives(),
            "body": mail.body[:2000],
            "attachments": mail.attachments,
        }, ensure_ascii=False, indent=1))
        return 0

    config = load_config(args.config)

    if args.command == "poll":
        for result in poll(config, dry_run=args.dry_run, limit=args.limit):
            print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0

    if args.command == "watch":
        send_startup_note(config)
        interval = int(config.get("gateway", {}).get("poll_interval_seconds", 300))
        while True:
            try:
                for result in poll(config, dry_run=args.dry_run):
                    print(json.dumps(result, ensure_ascii=False))
                for result in notify_finished(config):
                    print(json.dumps(result, ensure_ascii=False))
            except Exception as exc:
                LOG.exception("Poll failed: %s", exc)
            time.sleep(interval)

    if args.command == "notify":
        for result in notify_finished(config):
            print(json.dumps(result, ensure_ascii=False))
        return 0

    if args.command == "run":
        send_startup_note(config)
        exit_code = 0
        try:
            for result in poll(config):
                print(json.dumps(result, ensure_ascii=False))
        except Exception as exc:
            LOG.exception("Poll failed: %s", exc)
            exit_code = 1
        for result in notify_finished(config):
            print(json.dumps(result, ensure_ascii=False))
        return exit_code

    if args.command == "send":
        send_email(config, args.to, args.subject, args.body)
        print("sent")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
