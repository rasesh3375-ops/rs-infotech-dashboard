#!/usr/bin/env python3
"""
Emails the owner one day's cash transactions, every morning, from the
figures the Tally sync has already stored in Firestore (daily_reports).

Run by .github/workflows/daily-cash-email.yml on GitHub's own servers, so
it goes out whether or not any office PC or laptop is switched on. It only
reads the database and never talks to Tally: if the day hasn't been synced
the email says so, rather than reporting Rs.0 for a day nobody checked.

Settings come from the repository's secrets, never from this file -- the
repository is public:
  FIREBASE_SERVICE_ACCOUNT  the whole service-account.json, to read Firestore
  GMAIL_USER                the Gmail address the email is sent from
  GMAIL_APP_PASSWORD        a Gmail app password for it (not the normal one)
  MAIL_TO                   where it goes; several addresses separated by commas

Usage:
  python daily_cash_email.py               # yesterday, India time
  python daily_cash_email.py 2026-10-03    # a given day
  python daily_cash_email.py --dry-run     # print the email, send nothing
"""

import datetime
import html
import json
import os
import smtplib
import sys
from email.message import EmailMessage
from zoneinfo import ZoneInfo

DASHBOARD_URL = "https://rs-infotech-dashboard.web.app"
LOGO_URL = DASHBOARD_URL + "/logo-mark.png"
COLLECTION = "daily_reports"
IST = ZoneInfo("Asia/Kolkata")


def inr(n):
    """Whole rupees with Indian digit grouping (1,23,45,678), like the dashboard."""
    n = round(n or 0)
    sign, s = ("-" if n < 0 else ""), str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    return f"{sign}₹{s}"


def build_email(day, report):
    """(subject, plain text, html) for one day. report is the day's
    daily_reports document, or None when that day was never synced."""
    label = day.strftime("%A, %d %b %Y")
    if report is None:
        subject = f"Cash transactions {day:%d %b %Y} -- not synced yet"
        text = (f"R. S. Infotech -- cash transactions for {label}\n\n"
                "This day hasn't been synced from Tally yet, so there are no figures to send. "
                "The Tally PC may have been off or Tally closed at the 10 AM sync.\n\n" + DASHBOARD_URL)
        body = (f"<p>This day hasn't been synced from Tally yet, so there are no figures to send. "
                f"The Tally PC may have been off or Tally closed at the 10 AM sync.</p>")
        return subject, text, _wrap(label, body)

    rows = (report.get("cash_vouchers") or {}).get("vouchers") or []
    cash_in = sum(v.get("amount") or 0 for v in rows if v.get("direction") == "cash_in")
    cash_out = sum(v.get("amount") or 0 for v in rows if v.get("direction") != "cash_in")
    net = cash_in - cash_out
    # Opening and closing cash in hand are Tally's own (sync_tally.py,
    # _cash_balance_for), shown only when they tie to the day's entries.
    # A day synced before they were stored, or one that didn't tie, says
    # so instead of showing a figure nobody can trust.
    bal = report.get("cash_balance") or {}
    balances_ok = bal.get("matches") is True
    if balances_ok:
        balance_note = ""
    elif "matches" in bal:
        balance_note = "Opening and closing balance couldn't be confirmed against Tally for this day -- check its Cash Book."
    else:
        balance_note = "Opening and closing balance appear once this day is re-synced from Tally."
    subject = (f"Cash {day:%d %b %Y}: " + (f"Opening {inr(bal['opening'])}, " if balances_ok else "") +
               f"In {inr(cash_in)}, Out {inr(cash_out)}" + (f", Closing {inr(bal['closing'])}" if balances_ok else ""))

    lines = [f"R. S. Infotech -- cash transactions for {label}", ""]
    if balances_ok:
        lines.append(f"Opening: {inr(bal['opening'])}")
    lines += [f"In:      {inr(cash_in)}", f"Out:     {inr(cash_out)}"]
    lines.append(f"Closing: {inr(bal['closing'])}" if balances_ok else f"Net:     {inr(net)}")
    lines += [f"Vouchers: {len(rows)}"] + ([balance_note] if balance_note else []) + [""]
    for v in rows:
        lines.append(f"{'IN ' if v.get('direction') == 'cash_in' else 'OUT'}  {inr(v.get('amount')):>12}  "
                     f"{v.get('party') or '(no party)'} -- {v.get('description') or ''} ({v.get('type') or ''})")
    if not rows:
        lines.append("No cash vouchers on this day.")
    lines += ["", DASHBOARD_URL]

    e = html.escape
    cell = 'style="padding:6px 8px;border-bottom:1px solid #e5e1dc;vertical-align:top"'
    num = 'style="padding:6px 8px;border-bottom:1px solid #e5e1dc;text-align:right;white-space:nowrap;font-family:Consolas,monospace"'
    table = "".join(
        f"<tr><td {cell}><b>{e(v.get('party') or '(no party)')}</b><br>"
        f"<span style=\"color:#6e6358;font-size:12px\">{e(v.get('description') or '')}</span></td>"
        f"<td {cell}>{e(v.get('type') or '')}</td>"
        f"<td {cell}><span style=\"font-weight:700;color:{'#167a51' if v.get('direction') == 'cash_in' else '#d31a14'}\">"
        f"{'In' if v.get('direction') == 'cash_in' else 'Out'}</span></td>"
        f"<td {num}>{e(inr(v.get('amount')))}</td></tr>" for v in rows)
    if not rows:
        table = f'<tr><td {cell} colspan="4">No cash vouchers on this day.</td></tr>'
    box = 'style="padding:10px 14px;border:1px solid #e5e1dc;border-radius:8px"'
    tile = lambda title, value, color="#161311": (
        f'<td {box}><div style="color:#6e6358;font-size:12px">{title}</div>'
        f'<div style="font-size:20px;font-weight:800;color:{color}">{e(value)}</div></td>')
    tiles = ((tile("OPENING", inr(bal["opening"])) if balances_ok else "") +
             tile("IN", inr(cash_in), "#167a51") + tile("OUT", inr(cash_out), "#d31a14") +
             (tile("CLOSING", inr(bal["closing"])) if balances_ok else tile("NET", inr(net))))
    body = (
        f'<table style="border-collapse:separate;border-spacing:8px 0;margin:0 -8px 6px"><tr>{tiles}</tr></table>'
        f'<p style="margin:0 0 12px;font-size:12.5px;color:#6e6358">{len(rows)} voucher{"s" if len(rows) != 1 else ""}'
        + (f' · <span style="color:#d31a14">{e(balance_note)}</span>' if balance_note else "") + '</p>'
        '<table style="border-collapse:collapse;width:100%;font-size:13.5px">'
        '<tr style="background:#eef2f8;text-align:left"><th style="padding:6px 8px">Party</th><th style="padding:6px 8px">Type</th>'
        '<th style="padding:6px 8px"></th><th style="padding:6px 8px;text-align:right">Amount</th></tr>'
        f'{table}</table>')
    return subject, "\n".join(lines), _wrap(label, body)


def _wrap(label, body):
    return (
        '<div style="font-family:Segoe UI,Arial,sans-serif;color:#161311;max-width:640px">'
        f'<div style="display:flex;align-items:center;gap:10px;border-bottom:2px solid #14539a;padding-bottom:8px;margin-bottom:12px">'
        f'<img src="{LOGO_URL}" alt="" height="34" style="height:34px;margin-right:10px;vertical-align:middle">'
        '<span style="vertical-align:middle"><b style="font-size:17px">R. S. Infotech</b><br>'
        f'<span style="font-size:13px;color:#6e6358">Cash transactions · {html.escape(label)}</span></span></div>'
        f'{body}'
        f'<p style="margin-top:16px;font-size:12px;color:#6e6358">From the dashboard: <a href="{DASHBOARD_URL}">{DASHBOARD_URL}</a></p>'
        '</div>')


def read_report(day):
    import firebase_admin
    from firebase_admin import credentials, firestore
    firebase_admin.initialize_app(credentials.Certificate(json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"])))
    snap = firestore.client().collection(COLLECTION).document(day.isoformat()).get()
    return snap.to_dict() if snap.exists else None


def send(subject, text, html_body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"R. S. Infotech Dashboard <{os.environ['GMAIL_USER']}>"
    msg["To"] = os.environ["MAIL_TO"]
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    host, port = os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "465"))
    if os.environ.get("SMTP_PLAIN"):          # local testing against a stand-in server only
        with smtplib.SMTP(host, port) as s:
            s.send_message(msg)
        return
    with smtplib.SMTP_SSL(host, port) as s:
        s.login(os.environ["GMAIL_USER"], os.environ["GMAIL_APP_PASSWORD"].replace(" ", ""))
        s.send_message(msg)


def main():
    # Until the four secrets are added the scheduled run would fail every
    # morning, and each failure is an email from GitHub; say what's missing
    # once in the run's summary and stop quietly instead.
    missing = [k for k in ("FIREBASE_SERVICE_ACCOUNT", "GMAIL_USER", "GMAIL_APP_PASSWORD", "MAIL_TO") if not os.environ.get(k)]
    if missing and "--dry-run" not in sys.argv:
        print(f"::warning::Daily cash email not sent -- repository secret(s) not set yet: {', '.join(missing)}")
        return
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    day = (datetime.date.fromisoformat(args[0]) if args and args[0]
           else datetime.datetime.now(IST).date() - datetime.timedelta(days=1))
    subject, text, html_body = build_email(day, read_report(day))
    if "--dry-run" in sys.argv:
        print(subject + "\n\n" + text)
        return
    send(subject, text, html_body)
    print(f"Sent: {subject}")


if __name__ == "__main__":
    main()
