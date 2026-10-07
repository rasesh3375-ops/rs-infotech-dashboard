#!/usr/bin/env python3
"""
Emails the owner the day's reports every evening at 6:45 PM, one email each, from the
figures the Tally sync has already stored in Firestore (daily_reports):

  Daily Cash Transactions   opening, in, out, closing cash in hand, every voucher
  Daily Bank Transactions   the same for the bank accounts, per bank
  Daily Purchase Entries    purchases before and with GST, every entry
  Daily Sales Entries       sales before and with GST, every entry

every Saturday, three more, and one every Monday:

  Pending Delivery Challans   every Delivery Note still in Tally, oldest first
  Pending Proforma Invoices   every Proforma Invoice still in Tally, oldest first
  Stock Summary               every item in stock, as on the Stock tile
  Debtors Pending 60+ Days    (Monday) every debtor whose oldest unpaid bill is 60+ days old
  Collection Call List        (Monday) the debtors to ring first, each with a payment
                              reminder ready to send -- worded by AI when
                              ANTHROPIC_API_KEY is set, see _call_list_report

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
  ANTHROPIC_API_KEY         optional: the Claude API key that words the call
                            list's reminders; without it they're standard wording

Usage:
  python daily_report_emails.py                        # the four daily ones, for today (India time)
  python daily_report_emails.py 2026-10-03             # the four daily ones, for a given day
  python daily_report_emails.py --report=weekly        # the two pending lists and the stock, as they stand now
  python daily_report_emails.py --report=monday        # the debtors pending 60+ days and the call list
  python daily_report_emails.py --report=cash,bank     # only some of them
  python daily_report_emails.py --dry-run              # print them, send nothing
"""

import datetime
import html
import json
import os
import re
import smtplib
import sys
from email.message import EmailMessage
from zoneinfo import ZoneInfo

DASHBOARD_URL = "https://rs-infotech-dashboard.web.app"
LOGO_URL = DASHBOARD_URL + "/logo-mark.png"
COLLECTION = "daily_reports"
CHALLAN_COLLECTION = "delivery_challans"
PERIOD_COLLECTION = "period_reports"
PENDING_PROFORMA_DOC = "pending_proforma_invoices"
STOCK_DOC = "stock_summary"
PARTY_BALANCES_DOC = "sundry_balances"
# The weekly debtors email lists a debtor whose oldest unpaid bill is at
# least this old; the sync adds up each one's bills this old as "over_60".
DEBTOR_OVERDUE_DAYS = 60
# A pending item at least this old is counted out separately, so the ones
# that have been waiting a month stand out from this week's.
OVERDUE_DAYS = 30
IST = ZoneInfo("Asia/Kolkata")

GREEN, RED = "#167a51", "#d31a14"
NAVY, INK, DIM, LINE, ZEBRA = "#0f3d75", "#1b2430", "#5b6573", "#e3e7ee", "#f6f8fb"
FONT = "Segoe UI,Helvetica,Arial,sans-serif"
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


def nice_date(iso):
    """2026-06-09 -> 09 Jun 2026, as people write it here."""
    try:
        return datetime.date.fromisoformat(iso).strftime("%d %b %Y")
    except (TypeError, ValueError):
        return iso or ""


# --- building blocks shared by every report ---------------------------------
#
# The layout chosen by the owner on 5 Oct 2026, after the first emails read
# badly in Outlook: a fixed 640 px white page with a navy header and a
# shaded-row table on a computer, and on a phone each row as its own block
# -- party and amount on one line, the rest underneath -- because a
# five-column table on an iPhone squeezes the descriptions to a word a line.
#
# Everything is laid out with tables and inline styles, the only layout
# desktop Outlook follows: its Word engine ignores max-width on a <div>,
# which is why the first version's table stretched across the whole window.
# The phone blocks are in the email too, hidden; the <style> block's media
# query swaps them for the table on a narrow screen. Desktop Outlook never
# applies media queries, so it always shows the table.
_STYLE = """<style>
@media only screen and (max-width: 620px) {
  .page { width: 100% !important; }
  .outer { padding: 0 !important; }
  .pad { padding-left: 16px !important; padding-right: 16px !important; }
  .tile { display: inline-block !important; width: 50% !important; box-sizing: border-box; padding-bottom: 8px !important; }
  .desk { display: none !important; }
  .mob { display: block !important; max-height: none !important; overflow: visible !important; }
}
</style>"""


def _tiles(items):
    """A row of figure boxes: items are (label, value, colour). On a phone
    they wrap two to a row."""
    w = int(100 / max(len(items), 1))
    cells = "".join(
        f'<td class="tile" width="{w}%" style="padding:0 4px;vertical-align:top">'
        f'<div style="background:{ZEBRA};border:1px solid {LINE};border-top:3px solid {NAVY if color == INK else color};'
        f'border-radius:4px;padding:9px 11px">'
        f'<div style="font-size:11px;letter-spacing:.4px;color:{DIM};font-weight:600">{e(label)}</div>'
        f'<div style="font-size:19px;font-weight:700;color:{color};margin-top:2px;white-space:nowrap">{e(value)}</div>'
        f'</div></td>'
        for label, value, color in items)
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="margin:0 0 10px"><tr>{cells}</tr></table>')


def _note(text, warn=False):
    return f'<p style="margin:0 0 12px;font-size:12px;color:{"#b42318" if warn else DIM}">{e(text)}</p>' if text else ""


def _cards(head, rows, right):
    """The phone version of _table: each row a block, the Party (or first)
    column and the amount on one line, the other columns under it. A row
    whose Days cell is red -- waiting OVERDUE_DAYS or more -- gets a red bar."""
    main = head.index("Party") if "Party" in head else 0
    amount = max(right) if right and head[max(right)] in ("Amount", "Closing", "Value", "Balance") else None
    out = ""
    for r in rows:
        if not any(str(x).strip() for j, x in enumerate(r) if j != main):
            out += f'<div style="padding:12px 0;color:{DIM};font-size:13px;border-top:1px solid {LINE}">{r[main]}</div>'
            continue
        meta = []
        for j, x in enumerate(r):
            if j in (main, amount) or not str(x).strip():
                continue
            if head[j] == "Days":
                meta.append(x.replace("d</span>", " days</span>"))
            elif head[j] in ("", "Type", "Date", "Bank", "Category", "Quantity"):
                meta.append(x)
            else:
                meta.append(f"{e(head[j])} {x}")
        hot = any(head[j] == "Days" and RED in r[j] for j in range(len(r)))
        price = (f'<td align="right" style="font-size:15px;font-weight:700;color:{INK};white-space:nowrap;'
                 f'vertical-align:top;padding-left:10px">{r[amount]}</td>') if amount is not None else ""
        out += (f'<div style="border-top:1px solid {LINE};padding:11px 0 11px '
                f'{"10px;border-left:3px solid " + RED if hot else "0"}">'
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
                f'<td style="font-size:14px;color:{INK};vertical-align:top">{r[main]}</td>{price}</tr></table>'
                f'<div style="font-size:12px;color:{DIM};margin-top:4px">{" &nbsp;·&nbsp; ".join(meta)}</div></div>')
    return out


def _table(head, rows, right=()):
    """head: column titles; rows: lists of HTML-ready cells; right: indexes
    of columns to right-align (amounts). The table on a computer, _cards on
    a phone -- both are in the email, see _STYLE."""
    th = "".join(
        f'<th style="background:{NAVY};color:#ffffff;font-size:11.5px;font-weight:600;letter-spacing:.3px;'
        f'padding:8px 10px;text-align:{"right" if i in right else "left"}">{e(h)}</th>'
        for i, h in enumerate(head))
    body = ""
    for n, r in enumerate(rows):
        bg = ZEBRA if n % 2 else "#ffffff"
        body += "<tr>" + "".join(
            f'<td style="background:{bg};padding:9px 10px;border-bottom:1px solid {LINE};vertical-align:top;font-size:13px;'
            f'{"text-align:right;white-space:nowrap;font-weight:600" if i in right else ""}">{x}</td>'
            for i, x in enumerate(r)) + "</tr>"
    return (f'<table class="desk" role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border-collapse:collapse;margin:6px 0 14px"><tr>{th}</tr>{body}</table>'
            # Desktop Outlook ignores display:none on the tables inside a
            # hidden <div>, so on 5 Oct it showed every stock item twice:
            # the table, then the phone blocks. The conditional comment
            # keeps the blocks out of Outlook altogether; every other
            # client reads straight through it.
            f'<!--[if !mso]><!--><div class="mob" style="display:none;max-height:0;overflow:hidden;margin:6px 0 14px">'
            f'{_cards(head, rows, right)}</div><!--<![endif]-->')


def _party_cell(v):
    return (f'<div style="font-weight:600">{e(v.get("party") or "(no party)")}</div>'
            f'<div style="color:{DIM};font-size:12px;margin-top:2px">{e(v.get("description") or "")}</div>')


def _date_cell(iso):
    return f'<span style="white-space:nowrap">{e(nice_date(iso))}</span>'


def _wrap(report_name, label, body):
    """The whole email: navy header, title, body, footer with the dashboard
    button, on a 640 px page."""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">{_STYLE}</head>
<body style="margin:0;padding:0;background:#eef1f5">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef1f5;font-family:{FONT}">
<tr><td class="outer" align="center" style="padding:24px 12px">
<table class="page" role="presentation" width="640" cellpadding="0" cellspacing="0" style="width:640px;background:#ffffff;border:1px solid #d9dee6">
<tr><td class="pad" style="background:{NAVY};padding:16px 24px">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
  <td style="vertical-align:middle"><img src="{LOGO_URL}" width="30" height="30" alt="" style="width:30px;height:30px;vertical-align:middle;background:#ffffff;border-radius:4px;padding:2px">
  <span style="color:#ffffff;font-size:17px;font-weight:700;vertical-align:middle;padding-left:10px">R. S. Infotech</span></td>
  <td align="right" style="color:#b9c8de;font-size:12px;vertical-align:middle">Report from Tally</td></tr></table>
</td></tr>
<tr><td class="pad" style="padding:22px 24px 4px">
  <div style="font-size:21px;font-weight:700;color:{INK}">{e(report_name)}</div>
  <div style="font-size:13px;color:{DIM};margin-top:3px">{e(label)}</div></td></tr>
<tr><td class="pad" style="padding:14px 20px 22px;color:{INK};font-size:13px">{body}</td></tr>
<tr><td class="pad" style="padding:16px 24px;background:#f7f9fb;border-top:1px solid {LINE}">
  <a href="{DASHBOARD_URL}" style="display:inline-block;background:{NAVY};color:#ffffff;text-decoration:none;font-size:13px;font-weight:600;padding:9px 16px;border-radius:4px">Open dashboard</a>
  <div style="font-size:11.5px;color:{DIM};margin-top:10px">Sent automatically from R. S. Infotech's Tally data.</div></td></tr>
</table></td></tr></table></body></html>"""


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

    tiles = ([("OPENING", inr(bal["opening"]), INK)] if ok else []) + [
        ("IN", inr(money_in), GREEN), ("OUT", inr(money_out), RED),
        ("CLOSING", inr(bal["closing"]), INK) if ok else ("NET", inr(money_in - money_out), INK)]
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
    tiles = ([("BEFORE GST", inr(before_gst), INK)] if known else []) + [
        ("WITH GST", inr(with_gst), INK), ("ENTRIES", str(len(rows)), INK)]
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


def _pending_report(rows, today, noun, with_amount):
    """Delivery Challans or Proforma Invoices still pending: (text lines,
    html body). Oldest first, each with how many days it has waited."""
    def age(r):
        try:
            return (today - datetime.date.fromisoformat(r.get("date") or "")).days
        except ValueError:
            return None
    rows = sorted(rows, key=lambda r: r.get("date") or "")
    overdue = [r for r in rows if (age(r) or 0) >= OVERDUE_DAYS]
    value = sum(r.get("amount") or 0 for r in rows)
    oldest = rows[0].get("date") if rows else ""

    lines = [f"Pending: {len(rows)}"] + ([f"Value with GST: {inr(value)}"] if with_amount else []) + [
        f"Waiting {OVERDUE_DAYS}+ days: {len(overdue)}"] + ([f"Oldest: {nice_date(oldest)}"] if oldest else []) + [""]
    for r in rows:
        a = age(r)
        lines.append(f"{nice_date(r.get('date')):<11}  {str(a) + 'd' if a is not None else '':>5}  "
                     + (f"{inr(r.get('amount')):>12}  " if with_amount else "")
                     + f"{r.get('party') or '(no party)'} -- {r.get('description') or ''} ({r.get('voucher_no') or ''})")
    if not rows:
        lines.append(f"No pending {noun}s.")

    tiles = [("PENDING", str(len(rows)), RED if rows else INK)]
    if with_amount:
        tiles.append(("VALUE WITH GST", inr(value), INK))
    tiles.append((f"{OVERDUE_DAYS}+ DAYS OLD", str(len(overdue)), RED if overdue else INK))
    body = _tiles(tiles) + _note("Oldest first. Days = days since its date in Tally." if rows else "")

    def age_cell(r):
        a = age(r)
        if a is None:
            return ""
        return f'<span style="font-weight:700;color:{RED if a >= OVERDUE_DAYS else DIM}">{a}d</span>'
    head = ["Date", "Days", "Party", "Voucher No"] + (["Amount"] if with_amount else [])
    table_rows = [[_date_cell(r.get("date")), age_cell(r), _party_cell(r), e(r.get("voucher_no") or "")]
                  + ([e(inr(r.get("amount")))] if with_amount else []) for r in rows]
    if not rows:
        table_rows = [[f"No pending {noun}s.", "", "", ""] + ([""] if with_amount else [])]
    body += _table(head, table_rows, right=(1, 4) if with_amount else (1,))
    return lines, body


def _rate(text):
    """A stock rate as Tally sends it ("3830.66", or "50000.00/Nos") with
    the same Indian grouping as every other figure, paise kept."""
    m = re.match(r"\s*(-?[\d,]*\.?\d+)(.*)$", text or "")
    if not m:
        return text or ""
    n = float(m.group(1).replace(",", ""))
    whole, paise = f"{abs(n):.2f}".split(".")
    return inr(-int(whole) if n < 0 else int(whole)) + "." + paise + m.group(2)


def _stock_qty(r):
    m = re.match(r"\s*(-?[\d,]*\.?\d+)", r.get("qty_text") or "")
    return float(m.group(1).replace(",", "")) if m else float(r.get("qty") or 0)


def _stock_rows(doc):
    """(items, below_zero, left_out) as the stock email, its PDF and the
    dashboard's Stock tile list them: an item is listed when it has a value,
    or a quantity below zero. Left out are items with nothing in stock and
    -- as the owner asked on 5 Oct, after the first email listed dozens of
    licences at Rs.0 -- items held at no value in Tally."""
    every = doc.get("items") or []
    items = [r for r in every if abs(r.get("value") or 0) >= 0.5 or _stock_qty(r) < 0]
    negative = [r for r in items if (r.get("value") or 0) < -0.5 or _stock_qty(r) < 0]
    return items, negative, len(every) - len(items)


def _in_stock(items, negative):
    """The items actually in stock -- the below-zero ones are listed on their
    own above and not again -- oldest stock first when the sync has worked
    out each one's age (sync_tally.py, add_stock_ages), then by value."""
    held = [r for r in items if r not in negative]
    return sorted(held, key=lambda r: (-(r.get("age_days") if r.get("age_days") is not None else -1),
                                       -(r.get("value") or 0)))


def _age_text(r):
    """("since" line, days) for an item's age: "In stock since 01 May 2026",
    or "Opening stock, before 01 Apr 2026" with "187+" days when this
    year's purchases don't account for all of it."""
    if r.get("age_days") is None:
        return "", ""
    if r.get("from_opening"):
        return f"Opening stock, before {nice_date(r.get('since'))}", f"{r['age_days']}+"
    return f"In stock since {nice_date(r.get('since'))}", str(r["age_days"])


def _stock_report(doc, today):
    """The stock as the sync last read it from Tally's Stock Category
    Summary (sync_tally.py, _sync_stock): (text lines, html body). Items
    with no stock or no value are left out (_stock_rows), as on the
    dashboard's Stock tile. Items below zero -- sold or issued before the
    purchase was entered -- are listed first, so they get looked at, and
    not again under the items in stock, which come oldest stock first with
    how many days each has been held (_in_stock)."""
    by_group = doc.get("level") == "group"
    items, negative, left_out = _stock_rows(doc)
    held = _in_stock(items, negative)
    aged = any(r.get("age_days") is not None for r in held)
    total = doc.get("total_value") or 0
    noun = "group" if by_group else "item"
    when = ""
    try:
        when = datetime.datetime.fromisoformat(doc["as_of"]).astimezone(IST).strftime("%d %b, %I:%M %p")
    except (KeyError, TypeError, ValueError):
        pass
    notes = [(f"Read from Tally on {when}." if when else "")
             + (f" {left_out} {noun}s with no stock or no value aren't listed." if left_out else "")]
    if aged:
        notes.append("Days = how long the oldest of each item in hand has been in stock, from its purchase "
                     "entries, first in first out. Oldest stock first.")
    if doc.get("matches") is False:
        notes.append(f"Tally's P&L closing stock is {inr(doc.get('pl_closing_stock'))} -- check in Tally.")
    if doc.get("error"):
        notes.append(f"The latest read from Tally failed ({doc['error']}); these are the last figures read.")

    lines = [f"Stock value: {inr(total)}", f"{noun.capitalize()}s in stock: {len(held)}",
             f"Below zero: {len(negative)}"] + [n for n in notes if n] + [""]
    if negative:
        lines.append("Below zero -- check these in Tally:")
    for r in negative:
        lines.append(f"{inr(r.get('value')):>12}  {r.get('qty_text') or '':>9}  {r.get('name') or ''}")
    if negative:
        lines += ["", f"All {noun}s in stock:"]
    for r in held:
        since, days = _age_text(r)
        lines.append(f"{(days + 'd') if days else '':>6}  {inr(r.get('value')):>12}  {r.get('qty_text') or '':>9}  "
                     f"{r.get('name') or ''}" + (f" [{r['group']}]" if not by_group and r.get("group") else ""))
    if not held:
        lines.append("Nothing in stock.")

    tiles = [("STOCK VALUE", inr(total), INK), (f"{noun.upper()}S IN STOCK", str(len(held)), INK),
             ("BELOW ZERO", str(len(negative)), RED if negative else INK)]
    body = _tiles(tiles) + "".join(_note(n, warn="check" in n or "failed" in n) for n in notes if n)

    def red(r, x):
        return f'<span style="color:{RED}">{x}</span>' if r in negative else x
    if by_group:
        head, right = ["Stock group", "Quantity", "Value"], (1, 2)
        row = lambda r: [f"<b>{e(r.get('name') or '')}</b>", red(r, e(r.get("qty_text") or "")),
                         red(r, e(inr(r.get("value"))))]
        held_head, held_right, held_row = head, right, row
    else:
        head, right = ["Item", "Category", "Quantity", "Rate", "Value"], (2, 3, 4)
        row = lambda r: [f"<b>{e(r.get('name') or '')}</b>", e(r.get("group") or ""),
                         red(r, e(r.get("qty_text") or "")), e(_rate(r.get("rate_text"))),
                         red(r, e(inr(r.get("value"))))]
        if aged:
            # Days before Value: on a phone the last column is the amount on
            # the right of each block (_cards).
            held_head, held_right = ["Item", "Category", "Quantity", "Rate", "Days", "Value"], (2, 3, 4, 5)

            def held_row(r):
                since, days = _age_text(r)
                return [f"<b>{e(r.get('name') or '')}</b>"
                        + (f'<div style="color:{DIM};font-size:12px">{e(since)}</div>' if since else ""),
                        e(r.get("group") or ""), e(r.get("qty_text") or ""), e(_rate(r.get("rate_text"))),
                        f'<span style="font-weight:700">{e(days)}d</span>' if days else "",
                        e(inr(r.get("value")))]
        else:
            held_head, held_right, held_row = head, right, row
    if negative:
        body += (f'<div style="font-size:14px;font-weight:700;color:{RED};margin:14px 0 2px">'
                 f'Below zero -- check these in Tally</div>') + _table(head, [row(r) for r in negative], right)
        body += f'<div style="font-size:14px;font-weight:700;color:{INK};margin:14px 0 2px">All {noun}s in stock</div>'
    body += _table(held_head, [held_row(r) for r in held] or [["Nothing in stock."] + [""] * (len(held_head) - 1)],
                   held_right)
    return lines, body


def _overdue_debtors(doc, today):
    """The debtors the Monday email is about, oldest first: every debtor
    whose oldest unpaid bill is DEBTOR_OVERDUE_DAYS or more days old, with
    "days" counted to today and, when the sync stored the bills
    (sync_tally.py, _add_days_pending), "late_bills": those of its bills
    that old, each with its number, date, days, amount and what the invoice
    was for, and "late_value", what they add up to. Returns (debtors, how
    many debtors with a balance have no bill dates, whether bills are known)."""
    parties = (doc.get("debtors") or {}).get("parties") or []
    late = []
    for r in parties:
        try:
            days = (today - datetime.date.fromisoformat(r.get("oldest") or "")).days
        except ValueError:
            continue
        if days < DEBTOR_OVERDUE_DAYS or (r.get("amount") or 0) <= 0.5:
            continue
        row = dict(r, days=days)
        if "bills_list" in r:
            row["late_bills"] = []
            for b in r.get("bills_list") or []:
                try:
                    bdays = (today - datetime.date.fromisoformat(b.get("date") or "")).days
                except ValueError:
                    continue
                if bdays >= DEBTOR_OVERDUE_DAYS:
                    row["late_bills"].append(dict(b, days=bdays))
            row["late_value"] = round(sum(b.get("amount") or 0 for b in row["late_bills"]), 2)
        else:
            row["late_value"] = r.get("over_60") if "over_60" in r else None
        late.append(row)
    late.sort(key=lambda r: (-r["days"], -(r.get("amount") or 0)))
    undated = sum(1 for r in parties if not r.get("oldest") and (r.get("amount") or 0) > 0.5)
    return late, undated, bool(late) and all("late_bills" in r for r in late)


def _overdue_debtors_report(doc, today):
    """Debtors pending DEBTOR_OVERDUE_DAYS+ days (_overdue_debtors): (text
    lines, html body). Each debtor is a line of its own -- its whole
    balance in Tally and how much of it is that late -- followed by those
    late bills, each with its number, date, days, amount and what it was
    for, so the follow-up call can name the invoice."""
    late, undated, with_bills = _overdue_debtors(doc, today)
    known = all(r.get("late_value") is not None for r in late)
    late_total = sum((r["late_value"] if known else r.get("amount")) or 0 for r in late)
    when = ""
    try:
        when = datetime.datetime.fromisoformat(doc["as_of"]).astimezone(IST).strftime("%d %b, %I:%M %p")
    except (KeyError, TypeError, ValueError):
        pass
    notes = ["Oldest first. Days = days since the bill's date in Tally"
             + (f"; read from Tally on {when}." if when else "."),
             (f"{undated} debtor{' with a balance is' if undated == 1 else 's with a balance are'} not kept "
              f"bill-by-bill in Tally, so {'it has' if undated == 1 else 'they have'} no bill dates and "
              f"can't be listed here.") if undated else ""]

    lines = [f"Debtors pending {DEBTOR_OVERDUE_DAYS}+ days: {len(late)}",
             f"{'Amount ' + str(DEBTOR_OVERDUE_DAYS) + '+ days old' if known else 'Their balance'}: {inr(late_total)}"
             ] + [n for n in notes if n] + [""]
    for r in late:
        lines.append(f"{r.get('name')} -- balance {inr(r.get('amount'))}"
                     + (f", {inr(r['late_value'])} pending {DEBTOR_OVERDUE_DAYS}+ days" if known else "")
                     + f", oldest {r['days']} days")
        for b in r.get("late_bills") or []:
            lines.append(f"    {b.get('ref') or '':<12} {nice_date(b.get('date')):<11} {b['days']:>4}d "
                         f"{inr(b.get('amount')):>12}  {b.get('description') or ''}")
    if not late:
        lines.append(f"No debtor has a bill pending {DEBTOR_OVERDUE_DAYS} days or more.")

    tiles = [("DEBTORS", str(len(late)), RED if late else INK),
             (f"{DEBTOR_OVERDUE_DAYS}+ DAYS" if known else "THEIR BALANCE", inr(late_total), RED if late else INK),
             ("OLDEST", f"{late[0]['days']} days" if late else "-", RED if late else INK)]
    body = _tiles(tiles) + "".join(_note(n) for n in notes if n)
    if with_bills:
        head, right = ["Party / what the bill was for", "Bill No", "Date", "Days", "Amount"], (3, 4)
        rows = []
        for r in late:
            group = r.get("group") if r.get("group") not in ("Sundry Debtors", "Sub-group") else ""
            rows.append([f'<div style="font-weight:700;font-size:14px">{e(r.get("name") or "")}</div>'
                         f'<div style="color:{DIM};font-size:12px">Balance {e(inr(r.get("amount")))}'
                         f'{" · " + e(group) if group else ""}</div>', "", "", "",
                         f'<span style="font-weight:700;color:{RED}">{e(inr(r["late_value"]))}</span>'])
            for b in r["late_bills"]:
                rows.append([f'<span style="color:{DIM}">{e(b.get("description") or "-")}</span>',
                             e(b.get("ref") or ""), _date_cell(b.get("date")),
                             f'<span style="font-weight:700;color:{RED}">{b["days"]}d</span>',
                             e(inr(b.get("amount")))])
    else:
        head = ["Party", "Since", "Days"] + ([f"{DEBTOR_OVERDUE_DAYS}+ days"] if known else []) + ["Balance"]
        right = (2, 3, 4) if known else (2, 3)
        rows = [[_party_cell({"party": r.get("name"), "description": r.get("group") if r.get("group") != "Sundry Debtors" else ""}),
                 _date_cell(r.get("oldest")), f'<span style="font-weight:700;color:{RED}">{r["days"]}d</span>']
                + ([e(inr(r.get("late_value")))] if known else []) + [e(inr(r.get("amount")))] for r in late]
    if not rows:
        rows = [[f"No debtor has a bill pending {DEBTOR_OVERDUE_DAYS} days or more."] + [""] * (len(head) - 1)]
    body += _table(head, rows, right)
    return lines, body


def _pdf(title, today, summary, sections, footer_name):
    """A4 PDF of one or more tables, for the weekly emails to attach. Each
    section is a dict: heading (and heading_red), header, rows (lists of
    plain text), widths in mm, right_from (the first right-aligned
    column), wrap (columns that wrap), red (row indexes in red), bold (row
    indexes in bold, shaded -- a party's own line), total (a last row, or
    None). Page numbers on every page. The rupee sign needs a font that has
    it; DejaVu Sans is on GitHub's Ubuntu runners, and without it amounts
    read "Rs." instead."""
    import io
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    font, bold, rupee = "Helvetica", "Helvetica-Bold", "Rs."
    try:
        pdfmetrics.registerFont(TTFont("Body", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
        pdfmetrics.registerFont(TTFont("BodyBold", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))
        font, bold, rupee = "Body", "BodyBold", "₹"
    except Exception:
        pass
    fix = lambda t: (t or "").replace("₹", rupee)
    navy, red, dim = colors.HexColor(NAVY), colors.HexColor(RED), colors.HexColor(DIM)
    small = ParagraphStyle("small", fontName=font, fontSize=8.5, leading=10.5)
    small_bold = ParagraphStyle("smallb", parent=small, fontName=bold, fontSize=9)
    head_style = ParagraphStyle("h", fontName=bold, fontSize=16, leading=20, textColor=colors.HexColor(INK))
    sub = ParagraphStyle("s", fontName=font, fontSize=9.5, leading=13, textColor=dim)
    section_style = ParagraphStyle("sec", fontName=bold, fontSize=11, leading=15, spaceBefore=8, spaceAfter=3)

    def table(sec):
        bold_rows = set(sec.get("bold") or ())
        data = [sec["header"]]
        for i, r in enumerate(sec["rows"], start=1):
            data.append([Paragraph(e(fix(c)), small_bold if i in bold_rows else small) if j in sec.get("wrap", ())
                         else fix(c) for j, c in enumerate(r)])
        if sec.get("total"):
            data.append([fix(c) for c in sec["total"]])
        t = Table(data, colWidths=[w * mm for w in sec["widths"]], repeatRows=1)
        style = [("FONT", (0, 0), (-1, -1), font, 8.5), ("FONT", (0, 0), (-1, 0), bold, 8.5),
                 ("BACKGROUND", (0, 0), (-1, 0), navy), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                 ("ALIGN", (sec["right_from"], 0), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                 ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor(LINE)),
                 ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
        for i in range(1, len(sec["rows"]) + 1):
            if i in bold_rows:
                style += [("BACKGROUND", (0, i), (-1, i), colors.HexColor("#e8edf5")), ("FONT", (0, i), (-1, i), bold, 9)]
            elif not bold_rows and i % 2 == 0:
                style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor(ZEBRA)))
            if i in set(sec.get("red") or ()):
                style.append(("TEXTCOLOR", (sec["right_from"], i), (-1, i), red))
        if sec.get("total"):
            style += [("FONT", (0, -1), (-1, -1), bold, 9), ("LINEABOVE", (0, -1), (-1, -1), 1, navy)]
        t.setStyle(TableStyle(style))
        return t

    story = [Paragraph(e(f"R. S. Infotech – {title}"), head_style)]
    story += [Paragraph(fix(line), sub) for line in summary]
    for sec in sections:
        if sec.get("heading"):
            story.append(Paragraph(e(sec["heading"]), ParagraphStyle("hh", parent=section_style,
                                                                    textColor=red if sec.get("heading_red") else colors.HexColor(INK))))
        else:
            story.append(Spacer(1, 6))
        story.append(table(sec))

    def footer(canvas, d):
        canvas.saveState()
        canvas.setFont(font, 7.5)
        canvas.setFillColor(dim)
        canvas.drawString(15 * mm, 10 * mm, f"R. S. Infotech – {footer_name} as on {today:%d %b %Y}")
        canvas.drawRightString(A4[0] - 15 * mm, 10 * mm, f"Page {d.page}")
        canvas.restoreState()

    out = io.BytesIO()
    SimpleDocTemplate(out, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm,
                      bottomMargin=16 * mm, title=f"{title} {today:%d %b %Y}",
                      author="R. S. Infotech").build(story, onFirstPage=footer, onLaterPages=footer)
    return out.getvalue()


def stock_pdf(doc, today):
    """The stock email's list as a PDF: the same items (_stock_rows),
    below-zero ones first in red, then the items in stock -- not the
    below-zero ones again -- oldest stock first with the date and days
    each has been held (_in_stock), with the total."""
    by_group = doc.get("level") == "group"
    items, negative, left_out = _stock_rows(doc)
    held = _in_stock(items, negative)
    aged = any(r.get("age_days") is not None for r in held)
    noun = "group" if by_group else "item"
    if by_group:
        header, widths, right_from = ["Stock group", "Quantity", "Value"], [100, 35, 45], 1
        cells = lambda r: [r.get("name") or "", r.get("qty_text") or "", inr(r.get("value"))]
        held_header, held_widths, held_cells = header, widths, cells
    else:
        header, widths, right_from = ["Item", "Category", "Quantity", "Rate", "Value"], [78, 22, 22, 28, 30], 2
        cells = lambda r: [r.get("name") or "", r.get("group") or "", r.get("qty_text") or "",
                           _rate(r.get("rate_text")), inr(r.get("value"))]
        if aged:
            held_header, held_widths = ["Item", "Category", "Quantity", "Rate", "Since", "Days", "Value"], \
                [50, 18, 18, 24, 30, 14, 26]

            def held_cells(r):
                since, days = _age_text(r)
                return [r.get("name") or "", r.get("group") or "", r.get("qty_text") or "",
                        _rate(r.get("rate_text")),
                        ("Opening stock" if r.get("from_opening") else nice_date(r.get("since"))) if since else "",
                        days, inr(r.get("value"))]
        else:
            held_header, held_widths, held_cells = header, widths, cells
    when = ""
    try:
        when = datetime.datetime.fromisoformat(doc["as_of"]).astimezone(IST).strftime("%d %b %Y, %I:%M %p")
    except (KeyError, TypeError, ValueError):
        pass
    summary = [f"As on {today:%A, %d %b %Y}" + (f" · read from Tally {when}" if when else ""),
               f"Stock value {inr(doc.get('total_value') or 0)} · {len(held)} {noun}s in stock · "
               f"{len(negative)} below zero" + (f" · {left_out} with no stock or no value not listed" if left_out else "")]
    if aged:
        summary.append("Days = how long the oldest of each item in hand has been in stock (first in, first out). "
                       "Oldest stock first.")
    if doc.get("matches") is False:
        summary.append(f"Tally's P&L closing stock is {inr(doc.get('pl_closing_stock'))} – check in Tally.")
    wrap = (0, 1) if not by_group else (0,)
    sections = []
    if negative:
        sections.append(dict(heading="Below zero – check these in Tally", heading_red=True, header=header,
                             rows=[cells(r) for r in negative], widths=widths, right_from=right_from, wrap=wrap,
                             red=range(1, len(negative) + 1)))
    sections.append(dict(heading=f"All {noun}s in stock", header=held_header, rows=[held_cells(r) for r in held],
                         widths=held_widths, right_from=right_from, wrap=wrap,
                         total=["Total stock value"] + [""] * (len(held_header) - 2) + [inr(doc.get("total_value") or 0)]))
    return _pdf("Stock Summary", today, summary, sections, "Stock Summary")


def debtors_pdf(doc, today):
    """The Monday debtors email as a PDF: each debtor pending
    DEBTOR_OVERDUE_DAYS+ days on a shaded line of its own, with its late
    bills under it -- number, date, days, amount and what it was for."""
    late, undated, with_bills = _overdue_debtors(doc, today)
    known = all(r.get("late_value") is not None for r in late)
    total = sum((r["late_value"] if known else r.get("amount")) or 0 for r in late)
    summary = [f"As on {today:%A, %d %b %Y} · oldest first · days since the bill's date in Tally",
               f"{len(late)} debtors · {inr(total)} {'pending ' + str(DEBTOR_OVERDUE_DAYS) + '+ days' if known else 'balance'}"
               + (f" · oldest {late[0]['days']} days" if late else "")]
    if undated:
        summary.append(f"{undated} debtor{'' if undated == 1 else 's'} with a balance not kept bill-by-bill in Tally "
                       f"can't be listed (no bill dates).")
    if with_bills:
        header, widths, rows, bold = ["Party / what the bill was for", "Bill No", "Date", "Days", "Amount"], \
            [92, 24, 24, 14, 26], [], []
        for r in late:
            rows.append([f"{r.get('name') or ''}  (balance {inr(r.get('amount'))})", "", "", f"{r['days']}d",
                         inr(r["late_value"])])
            bold.append(len(rows))
            for b in r["late_bills"]:
                rows.append([b.get("description") or "-", b.get("ref") or "", nice_date(b.get("date")),
                             f"{b['days']}d", inr(b.get("amount"))])
        sec = dict(header=header, rows=rows, widths=widths, right_from=3, wrap=(0,), bold=bold,
                   total=["Total pending " + str(DEBTOR_OVERDUE_DAYS) + "+ days", "", "", "", inr(total)])
    else:
        header = ["Party", "Since", "Days"] + ([f"{DEBTOR_OVERDUE_DAYS}+ days"] if known else []) + ["Balance"]
        rows = [[r.get("name") or "", nice_date(r.get("oldest")), f"{r['days']}d"]
                + ([inr(r.get("late_value"))] if known else []) + [inr(r.get("amount"))] for r in late]
        sec = dict(header=header, rows=rows, widths=[80, 28, 18, 27, 27] if known else [95, 30, 20, 35],
                   right_from=2, wrap=(0,))
    return _pdf(f"Debtors Pending {DEBTOR_OVERDUE_DAYS}+ Days", today, summary, [sec],
                f"Debtors Pending {DEBTOR_OVERDUE_DAYS}+ Days")


# --- the collection call list -----------------------------------------------
#
# Monday's second email, next to the debtors pending 60+ days: who to ring
# first and, for each, a payment reminder ready to send on WhatsApp or by
# email. Asked for on 5 Oct 2026 as the first AI feature on the dashboard.
#
# Everything with a figure in it is worked out here, from Tally: who is on
# the list, the order, the bills, the amounts and the dates. The AI (Claude,
# through ANTHROPIC_API_KEY) only writes the wording around them -- how firm
# to be with a customer 200 days late against one at 35 -- and never sees a
# name, an amount, a bill number or a product. It returns a message with
# {NAME}, {BILLS} and {TOTAL} in it, which this file fills in; a message with
# any number of its own is thrown away for the plain wording below. A
# reminder with a wrong amount, sent to a customer, costs more than a dull
# one. With no key, or the AI unreachable, every message is the plain one
# and the email goes out the same.

# A bill this old is overdue on the call list -- the dashboard's
# PARTY_OVERDUE_DAYS, so the list and the Debtors tile's "pending 30+ days"
# agree.
CALL_LIST_MIN_DAYS = 30
# How many debtors the call list names: a week's calls, not all 80.
CALL_LIST_SIZE = 15
# How far back the last payment from each customer is looked for, in the
# days synced from Tally (daily_reports' bank and cash receipts).
RECEIPTS_LOOKBACK_DAYS = 365
AI_URL = "https://api.anthropic.com/v1/messages"
AI_MODEL = os.environ.get("AI_MODEL") or "claude-sonnet-5-5"
SIGN_OFF = "Accounts Team\nR. S. Infotech"
# Number words count as numbers: an AI message that spells out an amount
# is as wrong as one that writes it in digits.
_NUMBER_WORDS = re.compile(r"\b(lakhs?|lacs?|crores?|thousands?|hundreds?|percent|rupees?|rs)\b|[₹%#]", re.I)


def read_receipts(today):
    """{party name, lower case: [(date, amount), ...]} -- every payment
    received, by bank or in cash, in the last RECEIPTS_LOOKBACK_DAYS days
    that were synced (daily_reports' bank_vouchers and cash_vouchers going
    "in"). Only those two fields of each day are fetched."""
    start = (today - datetime.timedelta(days=RECEIPTS_LOOKBACK_DAYS)).isoformat()
    out = {}
    for snap in _db().collection(COLLECTION).select(["bank_vouchers", "cash_vouchers"]).stream():
        if not (start <= snap.id <= today.isoformat()):
            continue
        doc = snap.to_dict() or {}
        for key, way in (("bank_vouchers", "bank_in"), ("cash_vouchers", "cash_in")):
            for v in (doc.get(key) or {}).get("vouchers") or []:
                if v.get("direction") == way and (v.get("party") or "").strip() and (v.get("amount") or 0) > 0.5:
                    out.setdefault(v["party"].strip().lower(), []).append((snap.id, v["amount"]))
    return out


def _call_list(doc, today, receipts):
    """The debtors to call, most urgent first, and how many more there were
    than CALL_LIST_SIZE. Each one owes at least one bill CALL_LIST_MIN_DAYS
    or more days old (bills_list, sync_tally.py _add_days_pending) and is
    ranked by those bills' amount times their days past CALL_LIST_MIN_DAYS --
    so Rs.1.5 lakh 200 days old comes before Rs.7 lakh 40 days old, and both
    before Rs.5,000 300 days old.

    A sub-group's line on the Debtors tile holds several customers' bills;
    each customer is its own entry here, since each gets its own reminder.
    "check" is set when Tally's balance is below what the bills add up to
    -- a payment received on account and not set against a bill -- and then
    no message is offered, because the bills would ask for money already
    paid."""
    out = []
    for row in (doc.get("debtors") or {}).get("parties") or []:
        bills = row.get("bills_list") or []
        if not bills:
            continue
        by_party = {}
        for b in bills:
            by_party.setdefault((b.get("party") or row.get("name") or "").strip(), []).append(b)
        direct = list(by_party) == [(row.get("name") or "").strip()]
        if direct and (row.get("amount") or 0) <= 0.5:
            continue
        for name, owed in by_party.items():
            late = []
            for b in owed:
                try:
                    days = (today - datetime.date.fromisoformat(b.get("date") or "")).days
                except ValueError:
                    continue
                if days >= CALL_LIST_MIN_DAYS and (b.get("amount") or 0) > 0.5:
                    late.append(dict(b, days=days))
            if not late:
                continue
            late.sort(key=lambda b: (-b["days"], b.get("ref") or ""))
            late_value = round(sum(b["amount"] for b in late), 2)
            billed = round(sum(b.get("amount") or 0 for b in owed), 2)
            paid = None
            if receipts is not None:
                # Two receipts on one day are one payment as far as the call goes.
                days_paid = {}
                for d, amt in receipts.get(name.lower()) or []:
                    days_paid[d] = days_paid.get(d, 0) + amt
                paid = sorted(days_paid.items())
            check = ""
            if direct and (row.get("amount") or 0) < billed - 1:
                check = (f"Tally's balance is {inr(row.get('amount'))}, less than its bills' {inr(billed)} -- "
                         f"a payment is on account and not set against a bill. Settle that in Tally first; "
                         f"no message, as it would ask for money already paid.")
            out.append({
                "name": name, "group": "" if direct else row.get("name") or "",
                "balance": row.get("amount") if direct else None, "bills": late, "late_value": late_value,
                "days": late[0]["days"],
                "score": sum(b["amount"] * (b["days"] - CALL_LIST_MIN_DAYS + 1) for b in late),
                "last_paid": paid[-1] if paid else None, "paid_known": paid is not None, "check": check,
                "paid_days_ago": (today - datetime.date.fromisoformat(paid[-1][0])).days if paid else None,
                "paid_since_oldest": bool(paid) and paid[-1][0] >= late[0].get("date", "")})
    out.sort(key=lambda p: (-p["score"], p["name"].lower()))
    return out[:CALL_LIST_SIZE], max(len(out) - CALL_LIST_SIZE, 0)


def _bills_block(p):
    lines = []
    for b in p["bills"]:
        desc = (b.get("description") or "").strip()
        if len(desc) > 70:
            desc = desc[:67].rstrip(" ,") + "..."
        lines.append(f"• Invoice {b.get('ref') or '-'} dated {nice_date(b.get('date'))}: {inr(b['amount'])}"
                     + (f" for {desc}" if desc else ""))
    return "\n".join(lines)


def _plain_message(p):
    """The reminder with no AI: gentler for a customer a month late than for
    one four months late."""
    n = len(p["bills"])
    bills = "the following invoice is" if n == 1 else "the following invoices are"
    if p["days"] < 60:
        opening = f"This is a gentle reminder that {bills} still pending:"
        close = "We would be grateful if you could arrange the payment at the earliest."
    elif p["days"] < 120:
        opening = f"Our records show that {bills} overdue:"
        close = "Kindly arrange the payment this week, or let us know the date it will be released."
    else:
        opening = f"{bills[0].upper() + bills[1:]} long overdue and still pending:"
        close = ("We request you to clear this on priority. Please confirm the payment date, "
                 "or let us know if anything is holding it up so we can resolve it.")
    return f"Dear {{NAME}},\n\n{opening}\n\n{{BILLS}}\n\nTotal: {{TOTAL}}\n\n{close}\n\nThank you,\n{SIGN_OFF}"


def _plain_approach(p):
    if p["paid_since_oldest"]:
        return "They have paid since the oldest bill -- ask which bills that payment covered and when the rest will come."
    if p["days"] >= 120:
        return "Long overdue -- speak to the person who approves payments and ask for a firm date."
    if p["days"] >= 60:
        return "Ask for a payment date this week and note it."
    return "A friendly reminder call should do."


def _ai_facts(people):
    """What the AI is told about each customer: nothing that identifies
    them or any figure that goes in the message."""
    biggest = max((p["late_value"] for p in people), default=0) or 1
    facts = []
    for i, p in enumerate(people, 1):
        if not p["paid_known"]:
            paid = "unknown"
        elif not p["last_paid"]:
            paid = "no payment received in the last year"
        else:
            paid = (f"last payment received {p['paid_days_ago']} days ago, "
                    + ("after" if p["paid_since_oldest"] else "before") + " the oldest overdue bill")
        share = p["late_value"] / biggest
        facts.append({"id": f"C{i}", "oldest_overdue_days": p["days"], "overdue_bills": len(p["bills"]),
                      "amount_size": "large" if share >= 0.5 else "medium" if share >= 0.15 else "small",
                      "payment_history": paid})
    return facts


_AI_PROMPT = """You write payment reminders for R. S. Infotech, an IT products and services company in India, to customers whose invoices are overdue, and one line of advice for the person who will phone each customer.

For each customer below, return:
- "message": a short, polite, professional reminder in Indian business English, ready to send on WhatsApp or by email. Match the firmness to how late they are: friendly at around a month, clearly firm past two months, and past four months firm and asking for a definite payment date, while staying courteous -- they are customers. If they have paid something since the oldest bill, acknowledge it. Use these placeholders exactly once each and nothing else in braces: {NAME} for the customer's name (start with "Dear {NAME},"), {BILLS} on its own line where the list of invoices goes, {TOTAL} where the total amount due goes. End with exactly:
Accounts Team
R. S. Infotech
- "approach": one sentence, under 25 words, advising the caller how to handle this customer.

Rules, which are checked automatically: no digits anywhere, no amounts, no dates, no counts written in words, no invoice numbers, no rupee sign, no links. The figures are filled in afterwards from the accounts. Do not threaten legal action or interest.

Customers:
FACTS

Reply with only a JSON object mapping each id to {"message": ..., "approach": ...}."""


def _ask_claude(prompt):
    """The model's reply text, or raises. The key is a repository secret."""
    import urllib.request
    req = urllib.request.Request(AI_URL, method="POST", data=json.dumps({
        "model": AI_MODEL, "max_tokens": 8000,
        "messages": [{"role": "user", "content": prompt}]}).encode(),
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        reply = json.load(r)
    u = reply.get("usage") or {}
    print(f"AI: {AI_MODEL}, {u.get('input_tokens')} tokens in, {u.get('output_tokens')} out")
    return "".join(b.get("text", "") for b in reply.get("content") or [] if b.get("type") == "text")


def _ai_ok(message, approach):
    """Whether an AI message can be used: the three placeholders once each,
    no other braces, and nothing that is or reads as a figure -- the
    figures come only from Tally."""
    if any(message.count(k) != 1 for k in ("{NAME}", "{BILLS}", "{TOTAL}")):
        return False
    rest = message.replace("{NAME}", "").replace("{BILLS}", "").replace("{TOTAL}", "") + " " + approach
    return not (re.search(r"\d|[{}]|https?:|www\.", rest) or _NUMBER_WORDS.search(rest)
                or len(message) > 1500 or len(approach) > 250 or not approach.strip())


def _ai_drafts(people):
    """{index: (message, approach)} from the AI for the customers it wrote
    usable ones for, and a note on how it went for the email's footer."""
    if not people:
        return {}, ""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {}, "Messages are the standard wording: the AI is switched on by adding the ANTHROPIC_API_KEY secret."
    try:
        text = _ask_claude(_AI_PROMPT.replace("FACTS", json.dumps(_ai_facts(people), indent=1)))
        m = re.search(r"\{.*\}", text, re.S)
        reply = json.loads(m.group(0)) if m else {}
    except Exception as ex:
        print(f"::warning::AI messages not written, standard wording used: {ex}")
        return {}, "The AI couldn't be reached this week, so the messages are the standard wording."
    out = {}
    for i in range(len(people)):
        d = reply.get(f"C{i + 1}") if isinstance(reply, dict) else None
        if isinstance(d, dict) and isinstance(d.get("message"), str) and isinstance(d.get("approach"), str) \
                and _ai_ok(d["message"].strip(), d["approach"].strip()):
            out[i] = (d["message"].strip(), d["approach"].strip())
    kept = len(out)
    note = (f"Message wording and advice by AI ({kept} of {len(people)}; the rest are the standard wording). "
            f"The AI never sees names or figures: every amount, bill and date is filled in from Tally.")
    return out, note


def _fill(message, p):
    return (message.replace("{NAME}", p["name"]).replace("{BILLS}", _bills_block(p))
            .replace("{TOTAL}", inr(p["late_value"])))


def _paid_text(p):
    if not p["paid_known"]:
        return "Payment history couldn't be read"
    if not p["last_paid"]:
        return f"No payment received in the last {RECEIPTS_LOOKBACK_DAYS} days"
    d, amt = p["last_paid"]
    return f"Last payment {nice_date(d)} ({inr(amt)})"


def _button(href, label, color):
    return (f'<a href="{e(href)}" style="display:inline-block;background:{color};color:#ffffff;text-decoration:none;'
            f'font-size:13px;font-weight:600;padding:8px 14px;border-radius:4px;margin:8px 8px 0 0">{e(label)}</a>')


def _call_list_report(data, today):
    """The collection call list (_call_list): (text lines, html body). Each
    customer is a block -- what's overdue, since when, their last payment,
    how to approach the call, their overdue bills, and the reminder with a
    button to send it on WhatsApp (the phone asks which chat) or open it as
    an email."""
    import urllib.parse
    doc, receipts = data["balances"], data.get("receipts")
    people, more = _call_list(doc, today, receipts)
    drafts, ai_note = _ai_drafts([p for p in people if not p["check"]])
    sendable = [p for p in people if not p["check"]]
    for i, p in enumerate(sendable):
        msg, approach = drafts.get(i) or (_plain_message(p), _plain_approach(p))
        p["message"], p["approach"] = _fill(msg, p), approach
    total = sum(p["late_value"] for p in people)
    when = ""
    try:
        when = datetime.datetime.fromisoformat(doc["as_of"]).astimezone(IST).strftime("%d %b, %I:%M %p")
    except (KeyError, TypeError, ValueError):
        pass
    notes = [f"Most urgent first: each customer's bills {CALL_LIST_MIN_DAYS}+ days old, amount × days past {CALL_LIST_MIN_DAYS}. "
             f"Read from Tally{' on ' + when if when else ''}.",
             f"{more} more debtor{' has' if more == 1 else 's have'} bills {CALL_LIST_MIN_DAYS}+ days old; "
             f"every one {DEBTOR_OVERDUE_DAYS}+ days is in this morning's Debtors Pending {DEBTOR_OVERDUE_DAYS}+ Days."
             if more else "",
             "" if receipts is not None else "Payment history couldn't be read this week; the list itself is complete.",
             "WhatsApp asks which chat to send to; check each message before sending."]

    lines = [f"Customers to call: {len(people)}", f"Overdue {CALL_LIST_MIN_DAYS}+ days: {inr(total)}"] \
        + [n for n in notes if n] + [""]
    body = _tiles([("TO CALL", str(len(people)), RED if people else INK),
                   (f"OVERDUE {CALL_LIST_MIN_DAYS}+ DAYS", inr(total), RED if people else INK),
                   ("OLDEST", f"{max((p['days'] for p in people), default=0)} days" if people else "-",
                    RED if people else INK)])
    body += "".join(_note(n) for n in notes if n)
    if not people:
        lines.append(f"No debtor has a bill pending {CALL_LIST_MIN_DAYS} days or more.")
        body += _note(f"No debtor has a bill pending {CALL_LIST_MIN_DAYS} days or more.")
    for n, p in enumerate(people, 1):
        facts = f"Oldest {p['days']} days · {len(p['bills'])} bill{'s' if len(p['bills']) != 1 else ''} · {_paid_text(p)}"
        lines += [f"{n}. {p['name']} -- {inr(p['late_value'])} overdue" + (f" (under {p['group']})" if p["group"] else ""),
                  f"   {facts}"]
        block = (f'<div style="border-top:1px solid {LINE};padding:14px 0 16px">'
                 f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
                 f'<td style="font-size:15px;font-weight:700;color:{INK};vertical-align:top">{n}. {e(p["name"])}</td>'
                 f'<td align="right" style="font-size:16px;font-weight:700;color:{RED};white-space:nowrap;'
                 f'vertical-align:top;padding-left:10px">{e(inr(p["late_value"]))}</td></tr></table>'
                 f'<div style="font-size:12px;color:{DIM};margin-top:3px">{e(facts)}'
                 f'{" · under " + e(p["group"]) if p["group"] else ""}'
                 f'{" · Tally balance " + e(inr(p["balance"])) if p["balance"] is not None else ""}</div>')
        if p["check"]:
            lines += [f"   CHECK: {p['check']}", ""]
            block += (f'<div style="margin-top:8px;font-size:13px;color:#b42318">{e(p["check"])}</div>'
                      + "".join(f'<div style="font-size:12px;color:{DIM};margin-top:3px">{e(x)}</div>'
                                for x in _bills_block(p).split("\n")) + "</div>")
            body += block
            continue
        lines += [f"   Approach: {p['approach']}", "", *("   " + x for x in p["message"].split("\n")), ""]
        subject = f"Payment reminder -- R. S. Infotech"
        block += (f'<div style="margin-top:8px;font-size:13px;color:{NAVY}"><b>Approach:</b> {e(p["approach"])}</div>'
                  f'<div style="margin-top:10px;background:{ZEBRA};border:1px solid {LINE};border-left:3px solid {NAVY};'
                  f'border-radius:4px;padding:10px 12px;font-size:13px;line-height:1.45;color:{INK}">'
                  f'{e(p["message"]).replace(chr(10), "<br>")}</div>'
                  + _button("https://wa.me/?text=" + urllib.parse.quote(p["message"]), "Send on WhatsApp", GREEN)
                  + _button("mailto:?subject=" + urllib.parse.quote(subject) + "&body=" + urllib.parse.quote(p["message"]),
                            "Open as email", NAVY)
                  + "</div>")
        body += block
    if ai_note:
        lines.append(ai_note)
        body += _note(ai_note)
    return lines, body


# Every report: its email subject's name, whether it's a daily report of
# one day (reads that day's daily_reports document) or a list as it stands
# now, and how to build it.
REPORTS = {
    "cash": ("Daily Cash Transactions",
             lambda r: _money_report(r, "cash_vouchers", "cash_balance", "cash_in", "voucher", show_ledger=False)),
    "bank": ("Daily Bank Transactions",
             lambda r: _money_report(r, "bank_vouchers", "bank_balance", "bank_in", "transaction", show_ledger=True)),
    "purchase": ("Daily Purchase Entries", lambda r: _entries_report(r, "purchase", "purchase_accounts", "Purchase")),
    "sales": ("Daily Sales Entries", lambda r: _entries_report(r, "sales", "sales_accounts", "Sales")),
    "challans": ("Pending Delivery Challans",
                 lambda rows, today: _pending_report(rows, today, "delivery challan", with_amount=False)),
    "proformas": ("Pending Proforma Invoices",
                  lambda rows, today: _pending_report(rows, today, "proforma invoice", with_amount=True)),
    "stock": ("Stock Summary", _stock_report),
    "debtors": ("Debtors Pending 60+ Days", _overdue_debtors_report),
    "calls": ("Collection Call List", _call_list_report),
}
DAILY = ["cash", "bank", "purchase", "sales"]
# Lists as they stand now, dated the day they're sent (not one day's figures).
WEEKLY = ["challans", "proformas", "stock", "debtors", "calls"]
# Saturday's three, and the debtors on Monday morning -- moved there at the
# owner's request so the follow-up calls start the same week.
GROUPS = {"daily": DAILY, "weekly": ["challans", "proformas", "stock"], "monday": ["debtors", "calls"], "all": DAILY}


def build_email(kind, day, report, sync_note=""):
    """(subject, plain text, html) for one report and day. For a daily
    report, report is the day's daily_reports document, or None when that
    day was never synced; for a pending list it's the list (None when it
    was never synced) and day is the day it's sent. The subject is the
    report's name and the date, and only that. sync_note, for a daily
    report, says the Tally PC didn't answer this evening's sync request
    (request_sync) and goes at the top in red."""
    name, build = REPORTS[kind]
    label = day.strftime("%A, %d %b %Y")
    subject = f"{name} - {day:%d %b %Y}"
    if kind in WEEKLY:
        if report is None:
            msg = "This list hasn't been synced from Tally yet."
            return subject, f"R. S. Infotech -- {name} as on {label}\n\n{msg}\n\n{DASHBOARD_URL}", _wrap(name, label, f"<p>{e(msg)}</p>")
        lines, body = build(report, day)
        text = "\n".join([f"R. S. Infotech -- {name} as on {label}", ""] + lines + ["", DASHBOARD_URL])
        return subject, text, _wrap(name, "as on " + label, body)
    if report is None:
        msg = ("This day hasn't been synced from Tally yet, so there are no figures to send. "
               "The Tally PC may have been off or Tally closed all day.")
        return subject, f"R. S. Infotech -- {name} for {label}\n\n{msg}\n\n{DASHBOARD_URL}", _wrap(name, label, f"<p>{e(msg)}</p>")
    lines, body = build(report)
    # Sent the same evening, so say how up to date it is: an entry made in
    # Tally after this time isn't in it.
    when = _synced_when(report)
    synced = f"Synced from Tally at {when}." if when else ""
    head = [sync_note] if sync_note else []
    text = "\n".join([f"R. S. Infotech -- {name} for {label}", ""] + head + lines
                     + (["", synced] if synced else []) + ["", DASHBOARD_URL])
    return subject, text, _wrap(name, label, _note(sync_note, warn=True) + body + _note(synced))


def _synced_when(report):
    """"6:47 PM" (or "07 Oct, 6:47 PM" for another day) from a daily
    report's synced_at -- the Tally PC's own clock, India time, written
    without a zone."""
    try:
        t = datetime.datetime.fromisoformat(report["synced_at"])
    except (KeyError, TypeError, ValueError):
        return ""
    t = t.replace(tzinfo=IST) if t.tzinfo is None else t.astimezone(IST)
    clock = t.strftime("%I:%M %p").lstrip("0")
    return clock if t.date().isoformat() == report.get("date") else t.strftime("%d %b, ") + clock


# --- reading and sending ----------------------------------------------------

def read_report(day):
    snap = _db().collection(COLLECTION).document(day.isoformat()).get()
    return snap.to_dict() if snap.exists else None


def _db():
    import firebase_admin
    from firebase_admin import credentials, firestore
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"])))
    return firestore.client()


# How long the evening emails wait for the Tally PC to answer the sync
# request. A sync of today takes a minute or two; the PC's listener looks
# for a request every 20 seconds or so.
SYNC_WAIT_MINUTES = 15


def request_sync():
    """Asks the Tally PC to sync today now -- the dashboard's Sync now
    button, pressed by the 6:45 PM emails so entries made since the last
    sync are in them -- and waits for it to finish. Returns "" when it did,
    or a note for the top of each email saying why the figures may be
    behind; the emails go either way."""
    from firebase_admin import firestore
    import time
    import uuid
    control = _db().collection("sync_control")
    request_id = "email-" + uuid.uuid4().hex[:10]
    try:
        control.document("request").set({"request_id": request_id, "requested_at": firestore.SERVER_TIMESTAMP,
                                         "requested_by": "6:45 PM emails"})
    except Exception as ex:
        print(f"::warning::Sync request not sent: {ex}")
        return "These are the figures as last synced: the Tally PC couldn't be asked to sync before sending."
    deadline = time.time() + SYNC_WAIT_MINUTES * 60
    while time.time() < deadline:
        time.sleep(15)
        snap = control.document("status").get()
        st = (snap.to_dict() or {}) if snap.exists else {}
        if st.get("handled_request_id") == request_id and st.get("state") == "idle":
            print("Tally PC synced: " + ("ok" if st.get("ok") else f"FAILED -- {st.get('message')}"))
            return "" if st.get("ok") else ("The Tally PC's sync before sending failed, so these are the "
                                            "figures as last synced -- see the time at the bottom.")
    print(f"::warning::Tally PC didn't finish a sync within {SYNC_WAIT_MINUTES} minutes")
    return ("The Tally PC didn't answer the sync before sending (off, logged out or Tally closed), "
            "so these are the figures as last synced -- see the time at the bottom.")


def read_pending(kind):
    """The pending list as the sync last left it, or None if it never ran.
    Delivery Challans are one document each; the Proforma list is one
    document holding them all (sync_tally.py, push_pending_proformas), and
    the stock one document too (sync_tally.py, _sync_stock)."""
    if kind == "challans":
        return [d.to_dict() for d in _db().collection(CHALLAN_COLLECTION).stream()]
    if kind in ("debtors", "calls"):
        snap = _db().collection(PERIOD_COLLECTION).document(PARTY_BALANCES_DOC).get()
        doc = snap.to_dict() if snap.exists else None
        doc = doc if doc and (doc.get("debtors") or {}).get("bills_read") else None
        if kind == "debtors" or doc is None:
            return doc
        # The call list also says when each customer last paid. Without
        # that it still goes out, saying so (_call_list_report).
        try:
            receipts = read_receipts(datetime.datetime.now(IST).date())
        except Exception as ex:
            print(f"::warning::Payments received not read for the call list: {ex}")
            receipts = None
        return {"balances": doc, "receipts": receipts}
    if kind == "stock":
        snap = _db().collection(PERIOD_COLLECTION).document(STOCK_DOC).get()
        doc = snap.to_dict() if snap.exists else None
        return doc if doc and doc.get("as_of") else None
    snap = _db().collection(PERIOD_COLLECTION).document(PENDING_PROFORMA_DOC).get()
    return (snap.to_dict() or {}).get("proformas") or [] if snap.exists else None


def send(subject, text, html_body, attachments=()):
    """attachments: (file name, bytes) PDFs."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"R. S. Infotech Dashboard <{os.environ['GMAIL_USER']}>"
    msg["To"] = os.environ["MAIL_TO"]
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    for name, data in attachments:
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
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
    # day, and each failure is an email from GitHub; say what's missing
    # once in the run's summary and stop quietly instead.
    missing = [k for k in ("FIREBASE_SERVICE_ACCOUNT", "GMAIL_USER", "GMAIL_APP_PASSWORD", "MAIL_TO") if not os.environ.get(k)]
    if missing and not dry_run:
        print(f"::warning::Daily emails not sent -- repository secret(s) not set yet: {', '.join(missing)}")
        return 0
    kinds = list(DAILY)
    for a in sys.argv[1:]:
        if a.startswith("--report="):
            kinds = []
            for k in (k.strip() for k in a.split("=", 1)[1].split(",")):
                if k in GROUPS:
                    kinds += GROUPS[k]
                elif k in REPORTS:
                    kinds.append(k)
                elif k:
                    print(f"Unknown report: {k} -- choose from {', '.join(list(GROUPS) + list(REPORTS))}")
                    return 2
            kinds = list(dict.fromkeys(kinds)) or list(DAILY)
    dates = [a for a in sys.argv[1:] if not a.startswith("--") and a]
    today = datetime.datetime.now(IST).date()
    day = datetime.date.fromisoformat(dates[0]) if dates else today
    sync_note = ""
    if any(k in DAILY for k in kinds) and day == today and not dry_run:
        sync_note = request_sync()
    report = read_report(day) if any(k in DAILY for k in kinds) else None
    failed = []
    # One email per report; one failing to send doesn't stop the others.
    for kind in kinds:
        attachments = []
        if kind in WEEKLY:
            # A pending list is as it stands now, dated the day it's sent.
            listed = read_pending(kind)
            subject, text, html_body = build_email(kind, today, listed)
            if kind == "stock" and listed:
                attachments.append((f"Stock Summary {today:%d %b %Y}.pdf", stock_pdf(listed, today)))
            if kind == "debtors" and listed:
                attachments.append((f"Debtors Pending {DEBTOR_OVERDUE_DAYS}+ Days {today:%d %b %Y}.pdf",
                                    debtors_pdf(listed, today)))
        else:
            subject, text, html_body = build_email(kind, day, report, sync_note)
        if dry_run:
            print(f"=== {subject}\n{text}\n")
            continue
        try:
            send(subject, text, html_body, attachments)
            print(f"Sent: {subject}" + (f" with {', '.join(n for n, _ in attachments)}" if attachments else ""))
        except Exception as ex:
            print(f"::error::Could not send {subject}: {ex}")
            failed.append(kind)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
