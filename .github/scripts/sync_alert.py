#!/usr/bin/env python3
"""
Emails the owner when the Tally sync has stopped, and again when it's
working again -- so a PC that's switched off, logged out or has Tally
closed is noticed the same day, not when somebody wonders why the
dashboard looks old.

Run every hour during office hours by .github/workflows/sync-alert.yml.
It reads sync_control/status, where every sync records its outcome
(sync_tally.py, _record_result) and the active sync PC checks in every five
minutes, and keeps one small note of its own, sync_control/alert, so it
sends one email when a problem starts and one when it ends, not one an
hour.

Uses the same four repository secrets as the daily report emails.

Usage:
  python sync_alert.py             # check, and email if something changed
  python sync_alert.py --dry-run   # say what it would do, send nothing
"""

import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from daily_report_emails import IST, DASHBOARD_URL, _db, _wrap, _note, _tiles, send, e  # noqa: E402

# No successful sync for this long, during office hours, is a problem: the
# listener syncs at least hourly and a few minutes after every entry, and
# the daily run at 10 AM.
STALE_AFTER = datetime.timedelta(hours=2)


def _parse(iso):
    try:
        return datetime.datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None


def _when(t):
    return t.astimezone(IST).strftime("%d %b, %I:%M %p").replace(" 0", " ") if t else "never"


def assess(status, now):
    """(problem, details): problem is None when all is well, else one line
    saying what's wrong. details are (label, value) pairs for the email."""
    last_ok = _parse(status.get("last_ok_at"))
    seen = _parse(status.get("listener_seen_at"))
    details = [("Last good sync", f"{_when(last_ok)}" + (f" on {status['last_ok_host']}" if status.get("last_ok_host") else "")),
               ("Sync PC last checked in", f"{_when(seen)}" + (f" ({status['host']})" if status.get("host") else ""))]
    if status.get("last_error"):
        details.append(("Latest error", f"{status['last_error']} ({_when(_parse(status.get('last_error_at')))})"))
    if last_ok is None or now - last_ok > STALE_AFTER:
        if seen is None or now - seen > datetime.timedelta(minutes=30):
            why = "no sync PC has checked in -- the PCs may be off, asleep or logged out"
        elif status.get("last_error"):
            why = "the sync PC is on but the sync is failing -- most often Tally is closed or can't read its data"
        else:
            why = "the sync PC is on but nothing has synced"
        return f"Nothing has synced from Tally for over {int(STALE_AFTER.total_seconds() // 3600)} hours: {why}.", details
    return None, details


def build(problem, details, now):
    day = now.astimezone(IST)
    if problem:
        name, lead = "Tally Sync Alert", problem
    else:
        name, lead = "Tally Sync Working Again", "The Tally sync is working again; the dashboard is up to date."
    subject = f"{name} - {day:%d %b %Y}"
    text = "\n".join([f"R. S. Infotech -- {name}", "", lead, ""] + [f"{k}: {v}" for k, v in details] + ["", DASHBOARD_URL])
    rows = "".join(f'<tr><td style="padding:4px 12px 4px 0;color:#6e6358">{e(k)}</td><td style="padding:4px 0">{e(v)}</td></tr>'
                   for k, v in details)
    body = _note(lead, warn=bool(problem)) + f'<table style="font-size:13.5px">{rows}</table>'
    if problem:
        body += _note("Check that the accounts PC is on and logged in, and that Tally is open with R. S. Infotech loaded. "
                      "The Sync now button on the dashboard shows the latest state.")
    return subject, text, _wrap(name, f"{day:%A, %d %b %Y, %I:%M %p}", body)


def main():
    dry_run = "--dry-run" in sys.argv
    missing = [k for k in ("FIREBASE_SERVICE_ACCOUNT", "GMAIL_USER", "GMAIL_APP_PASSWORD", "MAIL_TO") if not os.environ.get(k)]
    if missing:
        print(f"::warning::Sync alert not checked -- repository secret(s) not set yet: {', '.join(missing)}")
        return 0
    control = _db().collection("sync_control")
    snap = control.document("status").get()
    status = (snap.to_dict() or {}) if snap.exists else {}
    alert_ref = control.document("alert")
    alert = (alert_ref.get().to_dict() or {}) if alert_ref.get().exists else {}
    now = datetime.datetime.now(datetime.timezone.utc)
    problem, details = assess(status, now)
    was_open = bool(alert.get("open"))
    print(f"Status: {problem or 'OK'} (alert {'open' if was_open else 'closed'})")
    if bool(problem) == was_open:
        return 0                      # nothing changed since the last email
    subject, text, html_body = build(problem, details, now)
    if dry_run:
        print(f"Would send: {subject}\n{text}")
        return 0
    send(subject, text, html_body)
    alert_ref.set({"open": bool(problem), "changed_at": now.isoformat(), "problem": problem or ""})
    print(f"Sent: {subject}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
