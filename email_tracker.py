"""
Email Tracker Agent — IMAP background poller.

Polls the configured Gmail inbox every N minutes, finds emails related to
tracked job applications (matched by company name or job role in subject/body),
classifies each email using GPT-4o, and writes events + status updates to the DB.

Classification labels:
  interview_invite   → status: interview
  offer              → status: offer
  rejection          → status: rejected
  assessment         → status: pending  (take-home / coding test)
  follow_up_request  → status: pending
  informational      → no status change
  other              → no status change

Usage:
  tracker = EmailTracker()
  tracker.start()   # launches background thread
  tracker.stop()
  tracker.status()  # returns dict
"""

import email
import imaplib
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from email.header import decode_header
from typing import Optional

from langchain_openai import ChatOpenAI

import config
import db

logger = logging.getLogger(__name__)


# ── Config ────────────────────────────────────────────────────────────────────

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
POLL_INTERVAL = 300   # 5 minutes
MAX_EMAILS_PER_POLL = 30


# ── Email helpers ─────────────────────────────────────────────────────────────

def _decode_header_str(raw) -> str:
    parts = decode_header(raw or "")
    result = []
    for b, enc in parts:
        if isinstance(b, bytes):
            result.append(b.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(b)
    return " ".join(result)


def _get_body(msg: email.message.Message) -> str:
    """Extract plain-text body (first 3000 chars)."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )[:3000]
                except Exception:
                    pass
    else:
        try:
            return msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8", errors="replace"
            )[:3000]
        except Exception:
            pass
    return ""


def _connect_imap(user: str, password: str) -> imaplib.IMAP4_SSL:
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(user, password)
    return mail


# ── GPT-4o classifier ─────────────────────────────────────────────────────────

_llm: Optional[ChatOpenAI] = None

def _get_llm():
    global _llm
    if _llm is None:
        _llm = ChatOpenAI(model="gpt-4o", temperature=0, api_key=config.OPENAI_API_KEY)
    return _llm


def _classify_email(subject: str, sender: str, body: str, company: str, role: str) -> dict:
    """
    Returns: {
      "label": "interview_invite" | "offer" | "rejection" | "assessment" |
                "follow_up_request" | "informational" | "other",
      "summary": "one sentence",
      "relevant": true/false
    }
    """
    prompt = f"""You are an email classifier for a job application tracker.

Application context:
  Company: {company}
  Role: {role}

Email:
  From: {sender}
  Subject: {subject}
  Body (truncated): {body[:1500]}

Classify this email. Return a JSON object with exactly these keys:
  "relevant": true if this email is about this job application or hiring process, false otherwise
  "label": one of: interview_invite | offer | rejection | assessment | follow_up_request | informational | other
  "summary": one sentence describing the email content

Output ONLY valid JSON, no other text."""

    try:
        resp = _get_llm().invoke(prompt)
        text = resp.content.strip()
        # Strip markdown code fences if present
        text = re.sub(r"^```json\s*|^```\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
        return json.loads(text)
    except Exception as e:
        logger.warning(f"Email classification failed: {e}")
        return {"relevant": False, "label": "other", "summary": "Classification failed."}


# ── Matching ──────────────────────────────────────────────────────────────────

def _find_matching_apps(subject: str, body: str, sender: str) -> list[dict]:
    """Return job applications whose company/role appears in the email."""
    apps = db.list_applications()
    matches = []
    text_lower = (subject + " " + body + " " + sender).lower()
    for app in apps:
        company = (app.get("company") or "").lower().strip()
        role = (app.get("role") or "").lower().strip()
        if company and len(company) > 2 and company in text_lower:
            matches.append(app)
            continue
        if role and len(role) > 3 and role in text_lower:
            matches.append(app)
    return matches


# ── Status updater ────────────────────────────────────────────────────────────

_LABEL_TO_STATUS = {
    "interview_invite":  "interview",
    "offer":             "offer",
    "rejection":         "rejected",
    "assessment":        None,
    "follow_up_request": None,
    "informational":     None,
    "other":             None,
}

_LABEL_TO_TITLE = {
    "interview_invite":  "Interview Invitation",
    "offer":             "Job Offer Received",
    "rejection":         "Rejection",
    "assessment":        "Assessment / Test Request",
    "follow_up_request": "Follow-up Request",
    "informational":     "Informational Email",
    "other":             "Email Received",
}


# ── Tracker class ─────────────────────────────────────────────────────────────

class EmailTracker:
    def __init__(self):
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_run: Optional[str] = None
        self._last_error: Optional[str] = None
        self._emails_processed = 0
        self._events_created = 0
        self._running = False

    def start(self):
        if self._running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="EmailTracker")
        self._thread.start()
        self._running = True
        logger.info("EmailTracker started.")

    def stop(self):
        self._stop_event.set()
        self._running = False
        logger.info("EmailTracker stopping…")

    def status(self) -> dict:
        return {
            "running": self._running,
            "last_run": self._last_run,
            "last_error": self._last_error,
            "emails_processed": self._emails_processed,
            "events_created": self._events_created,
            "configured": bool(config.SMTP_USER and config.SMTP_PASSWORD),
        }

    def _loop(self):
        while not self._stop_event.is_set():
            try:
                self._poll()
            except Exception as e:
                self._last_error = str(e)
                logger.error(f"EmailTracker poll error: {e}")
            self._stop_event.wait(POLL_INTERVAL)

    def _poll(self):
        user = config.SMTP_USER
        password = config.SMTP_PASSWORD
        if not user or not password:
            self._last_error = "SMTP_USER or SMTP_PASSWORD not configured."
            return

        self._last_run = datetime.now(timezone.utc).isoformat()
        self._last_error = None

        try:
            mail = _connect_imap(user, password)
        except Exception as e:
            raise RuntimeError(f"IMAP connection failed: {e}")

        try:
            mail.select("INBOX")
            # Search unseen emails from last 7 days
            _, data = mail.search(None, 'UNSEEN')
            uids = (data[0].split() if data[0] else [])[-MAX_EMAILS_PER_POLL:]

            for uid in uids:
                try:
                    _, msg_data = mail.fetch(uid, "(RFC822)")
                    raw = msg_data[0][1]
                    msg = email.message_from_bytes(raw)

                    subject = _decode_header_str(msg.get("Subject", ""))
                    sender  = _decode_header_str(msg.get("From", ""))
                    body    = _get_body(msg)

                    matched_apps = _find_matching_apps(subject, body, sender)
                    if not matched_apps:
                        self._emails_processed += 1
                        continue

                    for app in matched_apps:
                        classification = _classify_email(
                            subject, sender, body,
                            app.get("company", ""),
                            app.get("role", ""),
                        )

                        if not classification.get("relevant"):
                            continue

                        label   = classification.get("label", "other")
                        summary = classification.get("summary", subject)
                        title   = _LABEL_TO_TITLE.get(label, "Email Received")
                        new_status = _LABEL_TO_STATUS.get(label)

                        db.add_event(
                            app["id"],
                            label,
                            title,
                            body=f"From: {sender}\nSubject: {subject}\n\n{summary}",
                            source="email",
                            email_from=sender,
                            email_subj=subject,
                        )
                        self._events_created += 1

                        updates = {"last_email_at": datetime.now(timezone.utc).isoformat()}
                        if new_status:
                            updates["status"] = new_status
                        db.update_application(app["id"], updates)

                        logger.info(f"App {app['id']} ({app.get('company')}): {label} — {summary}")

                    self._emails_processed += 1

                except Exception as e:
                    logger.warning(f"Failed to process email {uid}: {e}")

        finally:
            try:
                mail.logout()
            except Exception:
                pass


# ── Singleton used by app.py ──────────────────────────────────────────────────

_tracker: Optional[EmailTracker] = None


def get_tracker() -> EmailTracker:
    global _tracker
    if _tracker is None:
        _tracker = EmailTracker()
    return _tracker
