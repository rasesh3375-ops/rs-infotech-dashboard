#!/usr/bin/env python3
"""
Emails the owner one day's reports every morning, one email each, from the
figures the Tally sync has already stored in Firestore (daily_reports):

  Daily Cash Transactions   opening, in, out, closing cash in hand, every voucher
  Daily Bank Transactions   the same for the bank accounts, per bank
  Daily Purchase Entries    purchases before and with GST, every entry
  Daily Sales Entries       sales before and with GST, every entry

Each email's subject is its report name and the date, nothing else, so the
four sort and search cleanly in the inbox.

Run by .github/workflows/daily-report-emails.yml on GitHub's own servers, so
they go out whether or not any office PC or laptop is switched on. It only
reads the database and never talks to Tally: if the day hasn't been synced
each email says so, rather than reporting Rs.0 for a day nobody checked.

Settings come from the repository's secrets, never from this file -- the
repository is public:
  FIREBASE_SERVICE_ACCOUNT  the whole service-account.json, to read Firestore
  GMAIL_USER                the Gmail address the emails are sent from
  GMAIL_APP_PASSWORD        a Gmail app password for it (not the normal one)
  MAIL_TO                   where they go; several addresses separated by commas

Usage:
  python daily_cash_email.py                      # all four, for yesterday (India time)
  python daily_cash_email.py 2026-10-03           # all four, for a given day
  python daily_cash_email.py --report=cash,bank   # only some of them
  python daily_cash_email.py --dry-run            # print them, send nothing
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

GREEN, RED, DIM, LINE = "#167a51", "#d31a14", "#6e6358", "#e5e1dc"
e = html.escape


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


# --- building blocks shared by every report ---------------------------------

def _tiles(items):
    """A row of figure boxes: items are (label, value, colour)."""
    box = f'style="padding:10px 14px;border:1px solid {LINE};border-radius:8px"'
    cells = "".join(
        f'<td {box}><div style="color:{DIM};font-size:12px">{e(label)}</div>'
        f'<div style="font-size:20px;font-weight:800;color:{color}">{e(value)}</div></td>'
        for label, value, color in items)
    return f'<table style="border-collapse:separate;border-spacing:8px 0;margin:0 -8px 6px"><tr>{cells}</tr></table>'


def _note(text, warn=False):
    return f'<p style="margin:0 0 12px;font-size:12.5px;color:{RED if warn else DIM}">{e(text)}</p>' if text else ""


def _table(head, rows, right=()):
    """head: column titles; rows: lists of HTML-ready cells; right: indexes
    of columns to right-align (amounts)."""
    th = "".join(f'<th style="padding:6px 8px;text-align:{"right" if i in right else "left"}">{e(h)}</th>'
                 for i, h in enumerate(head))
    def td(i, x):
        style = f"padding:6px 8px;border-bottom:1px solid {LINE};vertical-align:top"
        if i in right:
            style += ";text-align:right;white-space:nowrap;font-family:Consolas,monospace"
        return f'<td style="{style}">{x}</td>'
    body = "".join("<tr>" + "".join(td(i, x) for i, x in enumerate(r)) + "</tr>" for r in rows)
    return (f'<table style="border-collapse:collapse;width:100%;font-size:13.5px;margin-bottom:14px">'
            f'<tr style="background:#eef2f8">{th}</tr>{body}</table>')


def _party_cell(v):
    return (f"<b>{e(v.get('party') or '(no party)')}</b><br>"
            f"<span style=\"color:{DIM};font-size:12px\">{e(v.get('description') or '')}</span>")


def _wrap(report_name, label, body):
    return (
        '<div style="font-family:Segoe UI,Arial,sans-serif;color:#161311;max-width:680px">'
        '<div style="border-bottom:2px solid #14539a;padding-bottom:8px;margin-bottom:12px">'
        f'<img src="{LOGO_URL}" alt="" height="34" style="height:34px;margin-right:10px;vertical-align:middle">'
        '<span style="vertical-align:middle;display:inline-block"><b style="font-size:17px">R. S. Infotech</b><br>'
        f'<span style="font-size:13px;color:{DIM}">{e(report_name)} · {e(label)}</span></span></div>'
        f'{body}'
        f'<p style="margin-top:16px;font-size:12px;color:{DIM}">From the dashboard: <a href="{DASHBOARD_URL}">{DASHBOARD_URL}</a></p>'
        '</div>')


def _balance_state(bal):
    """(ok, note) for a stored cash_balance / bank_balance. The balances are
    Tally's own (sync_tally.py, _balance_for) and shown only when they tie
    to the day's entries; otherwise the email says why there are none,
    rather than showing a figure nobody can trust."""
    bal = bal or {}
    if bal.get("matches") is True:
        return True, ""
    if "matches" in bal:
        return False, "Opening and closing balance couldn't be confirmed against Tally for this day -- check its books."
    return False, "Opening and closing balance appear once this day is re-synced from Tally."


# --- the four reports ------------------------------------------------------

def _money_report(report, key, balance_key, in_dir, noun, show_ledger):
    """Cash or bank: (text lines, html body)."""
    rows = (report.get(key) or {}).get("vouchers") or []
    is_in = lambda v: v.get("direction") == in_dir
    money_in = sum(v.get("amount") or 0 for v in rows if is_in(v))
    money_out = sum(v.get("amount") or 0 for v in rows if not is_in(v))
    bal = report.get(balance_key) or {}
    ok, note = _balance_state(bal)

    lines = []
    if ok:
        lines.append(f"Opening: {inr(bal['opening'])}")
    lines += [f"In:      {inr(money_in)}", f"Out:     {inr(money_out)}",
              f"Closing: {inr(bal['closing'])}" if ok else f"Net:     {inr(money_in - money_out)}",
              f"{noun.capitalize()}s: {len(rows)}"] + ([note] if note else [])

    tiles = ([("OPENING", inr(bal["opening"]), "#161311")] if ok else []) + [
        ("IN", inr(money_in), GREEN), ("OUT", inr(money_out), RED),
        ("CLOSING", inr(bal["closing"]), "#161311") if ok else ("NET", inr(money_in - money_out), "#161311")]
    body = _tiles(tiles) + _note(f"{len(rows)} {noun}{'s' if len(rows) != 1 else ''}")
    if note:
        body += _note(note, warn=True)

    # With more than one bank account, each one's own opening and closing.
    per_ledger = bal.get("ledgers") or {}
    if ok and show_ledger and len(per_ledger) > 1:
        lines.append("")
        for name, b in per_ledger.items():
            lines.append(f"{name}: opening {inr(b['opening'])}, closing {inr(b['closing'])}")
        body += _table(["Account", "Opening", "Closing"],
                       [[e(name), e(inr(b["opening"])), e(inr(b["closing"]))] for name, b in per_ledger.items()],
                       right=(1, 2))

    lines.append("")
    for v in rows:
        lines.append(f"{'IN ' if is_in(v) else 'OUT'}  {inr(v.get('amount')):>12}  "
                     + (f"[{v.get('ledger')}] " if show_ledger and v.get("ledger") else "")
                     + f"{v.get('party') or '(no party)'} -- {v.get('description') or ''} ({v.get('type') or ''})")
    if not rows:
        lines.append(f"No {noun}s on this day.")

    direction = lambda v: (f'<span style="font-weight:700;color:{GREEN if is_in(v) else RED}">'
                           f'{"In" if is_in(v) else "Out"}</span>')
    head = ["Party", *(["Bank"] if show_ledger else []), "Type", "", "Amount"]
    table_rows = [[_party_cell(v), *([e(v.get("ledger") or "")] if show_ledger else []),
                   e(v.get("type") or ""), direction(v), e(inr(v.get("amount")))] for v in rows]
    if not rows:
        table_rows = [[f"No {noun}s on this day.", *([""] if show_ledger else []), "", "", ""]]
    body += _table(head, table_rows, right=(len(head) - 1,))
    return lines, body


def _entries_report(report, key, books_key, title):
    """Sales or purchase: (text lines, html body)."""
    x = report.get(key) or {}
    rows = x.get("vouchers") or []
    with_gst = sum(v.get("amount") or 0 for v in rows)
    pl = report.get("profit_and_loss") or {}
    before_gst = pl.get(books_key)
    known = isinstance(before_gst, (int, float))

    note = (f"Before GST is Tally's own {title} Accounts figure; with GST is the invoices' total."
            if known else "The before-GST figure appears once this day is re-synced from Tally.")
    lines = ([f"{title} before GST: {inr(before_gst)}  (Tally's {title} Accounts)"] if known else []) + [
        f"Total with GST:  {inr(with_gst)}", f"Entries: {len(rows)}"] + ([] if known else [note])
    tiles = ([("BEFORE GST", inr(before_gst), "#161311")] if known else []) + [
        ("WITH GST", inr(with_gst), "#161311"), ("ENTRIES", str(len(rows)), "#161311")]
    body = _tiles(tiles) + _note(note)

    lines.append("")
    for v in rows:
        lines.append(f"{inr(v.get('amount')):>12}  {v.get('party') or '(no party)'} -- "
                     f"{v.get('description') or ''} ({v.get('type') or ''}{', ' + v['voucher_no'] if v.get('voucher_no') else ''})")
    if not rows:
        lines.append(f"No {title.lower()} entries on this day.")
    table_rows = [[_party_cell(v), e(v.get("type") or ""), e(v.get("voucher_no") or ""), e(inr(v.get("amount")))]
                  for v in rows] or [[f"No {title.lower()} entries on this day.", "", "", ""]]
    body += _table(["Party", "Type", "Voucher No", "Amount"], table_rows, right=(3,))
    return lines, body


REPORTS = {
    "cash": ("Daily Cash Transactions",
             lambda r: _money_report(r, "cash_vouchers", "cash_balance", "cash_in", "voucher", show_ledger=False)),
    "bank": ("Daily Bank Transactions",
             lambda r: _money_report(r, "bank_vouchers", "bank_balance", "bank_in", "transaction", show_ledger=True)),
    "purchase": ("Daily Purchase Entries", lambda r: _entries_report(r, "purchase", "purchase_accounts", "Purchase")),
    "sales": ("Daily Sales Entries", lambda r: _entries_report(r, "sales", "sales_accounts", "Sales")),
}


def build_email(kind, day, report):
    """(subject, plain text, html) for one report and day. report is the
    day's daily_reports document, or None when that day was never synced.
    The subject is the report's name and the date, and only that."""
    name, build = REPORTS[kind]
    label = day.strftime("%A, %d %b %Y")
    subject = f"{name} - {day:%d %b %Y}"
    if report is None:
        msg = ("This day hasn't been synced from Tally yet, so there are no figures to send. "
               "The Tally PC may have been off or Tally closed at the 10 AM sync.")
        return subject, f"R. S. Infotech -- {name} for {label}\n\n{msg}\n\n{DASHBOARD_URL}", _wrap(name, label, f"<p>{e(msg)}</p>")
    lines, body = build(report)
    text = "\n".join([f"R. S. Infotech -- {name} for {label}", ""] + lines + ["", DASHBOARD_URL])
    return subject, text, _wrap(name, label, body)


# --- reading and sending ----------------------------------------------------

def read_report(day):
    import firebase_admin
    from firebase_admin import credentials, firestore
    if not firebase_admin._apps:
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
    dry_run = "--dry-run" in sys.argv
    # Until the four secrets are added the scheduled run would fail every
    # morning, and each failure is an email from GitHub; say what's missing
    # once in the run's summary and stop quietly instead.
    missing = [k for k in ("FIREBASE_SERVICE_ACCOUNT", "GMAIL_USER", "GMAIL_APP_PASSWORD", "MAIL_TO") if not os.environ.get(k)]
    if missing and not dry_run:
        print(f"::warning::Daily emails not sent -- repository secret(s) not set yet: {', '.join(missing)}")
        return 0
    kinds = list(REPORTS)
    for a in sys.argv[1:]:
        if a.startswith("--report="):
            wanted = [k.strip() for k in a.split("=", 1)[1].split(",") if k.strip() and k.strip() != "all"]
            unknown = [k for k in wanted if k not in REPORTS]
            if unknown:
                print(f"Unknown report(s): {', '.join(unknown)} -- choose from {', '.join(REPORTS)}")
                return 2
            kinds = wanted or kinds
    dates = [a for a in sys.argv[1:] if not a.startswith("--") and a]
    day = (datetime.date.fromisoformat(dates[0]) if dates
           else datetime.datetime.now(IST).date() - datetime.timedelta(days=1))
    report = read_report(day)
    failed = []
    # One email per report; one failing to send doesn't stop the others.
    for kind in kinds:
        subject, text, html_body = build_email(kind, day, report)
        if dry_run:
            print(f"=== {subject}\n{text}\n")
            continue
        try:
            send(subject, text, html_body)
            print(f"Sent: {subject}")
        except Exception as ex:
            print(f"::error::Could not send {subject}: {ex}")
            failed.append(kind)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
