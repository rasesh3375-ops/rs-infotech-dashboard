#!/usr/bin/env python3
"""
Emails the owner one day's reports every morning, one email each, from the
figures the Tally sync has already stored in Firestore (daily_reports):

  Daily Cash Transactions   opening, in, out, closing cash in hand, every voucher
  Daily Bank Transactions   the same for the bank accounts, per bank
  Daily Purchase Entries    purchases before and with GST, every entry
  Daily Sales Entries       sales before and with GST, every entry

and every Saturday, three more:

  Pending Delivery Challans   every Delivery Note still in Tally, oldest first
  Pending Proforma Invoices   every Proforma Invoice still in Tally, oldest first
  Stock Summary               every item in stock, as on the Stock tile

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
  python daily_report_emails.py                        # the four daily ones, for yesterday (India time)
  python daily_report_emails.py 2026-10-03             # the four daily ones, for a given day
  python daily_report_emails.py --report=weekly        # the two pending lists and the stock, as they stand now
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
    amount = max(right) if right and head[max(right)] in ("Amount", "Closing", "Value") else None
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


def _stock_report(doc, today):
    """The stock as the sync last read it from Tally's Stock Category
    Summary (sync_tally.py, _sync_stock): (text lines, html body). Items
    with no stock or no value are left out (_stock_rows), as on the
    dashboard's Stock tile; items below zero -- sold or issued before the purchase was
    entered -- are listed first, so they get looked at."""
    by_group = doc.get("level") == "group"
    items, negative, left_out = _stock_rows(doc)
    total = doc.get("total_value") or 0
    noun = "group" if by_group else "item"
    when = ""
    try:
        when = datetime.datetime.fromisoformat(doc["as_of"]).astimezone(IST).strftime("%d %b, %I:%M %p")
    except (KeyError, TypeError, ValueError):
        pass
    notes = [(f"Read from Tally on {when}." if when else "")
             + (f" {left_out} {noun}s with no stock or no value aren't listed." if left_out else "")]
    if doc.get("matches") is False:
        notes.append(f"Tally's P&L closing stock is {inr(doc.get('pl_closing_stock'))} -- check in Tally.")
    if doc.get("error"):
        notes.append(f"The latest read from Tally failed ({doc['error']}); these are the last figures read.")

    lines = [f"Stock value: {inr(total)}", f"{noun.capitalize()}s in stock: {len(items)}",
             f"Below zero: {len(negative)}"] + [n for n in notes if n] + [""]
    for r in negative + [r for r in items if r not in negative]:
        lines.append(f"{inr(r.get('value')):>12}  {r.get('qty_text') or '':>9}  {r.get('name') or ''}"
                     + (f" [{r['group']}]" if not by_group and r.get("group") else ""))
    if not items:
        lines.append("Nothing in stock.")

    tiles = [("STOCK VALUE", inr(total), INK), (f"{noun.upper()}S IN STOCK", str(len(items)), INK),
             ("BELOW ZERO", str(len(negative)), RED if negative else INK)]
    body = _tiles(tiles) + "".join(_note(n, warn="check" in n or "failed" in n) for n in notes if n)

    def red(r, x):
        return f'<span style="color:{RED}">{x}</span>' if r in negative else x
    if by_group:
        head, right = ["Stock group", "Quantity", "Value"], (1, 2)
        row = lambda r: [f"<b>{e(r.get('name') or '')}</b>", red(r, e(r.get("qty_text") or "")),
                         red(r, e(inr(r.get("value"))))]
    else:
        head, right = ["Item", "Category", "Quantity", "Rate", "Value"], (2, 3, 4)
        row = lambda r: [f"<b>{e(r.get('name') or '')}</b>", e(r.get("group") or ""),
                         red(r, e(r.get("qty_text") or "")), e(_rate(r.get("rate_text"))),
                         red(r, e(inr(r.get("value"))))]
    if negative:
        body += (f'<div style="font-size:14px;font-weight:700;color:{RED};margin:14px 0 2px">'
                 f'Below zero -- check these in Tally</div>') + _table(head, [row(r) for r in negative], right)
        body += f'<div style="font-size:14px;font-weight:700;color:{INK};margin:14px 0 2px">All {noun}s in stock</div>'
    body += _table(head, [row(r) for r in items] or [["Nothing in stock."] + [""] * (len(head) - 1)], right)
    return lines, body


def stock_pdf(doc, today):
    """The stock email's list as a PDF to attach: the same items
    (_stock_rows), below-zero ones first in red, then every item with its
    category, quantity, rate and value, with the total -- A4, page numbers
    on every page. The rupee sign needs a font that has it; DejaVu Sans is
    on GitHub's Ubuntu runners, and without it amounts read "Rs." instead."""
    import io
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    font, bold, rupee = "Helvetica", "Helvetica-Bold", "Rs."
    for regular, heavy in (("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),):
        try:
            pdfmetrics.registerFont(TTFont("Body", regular))
            pdfmetrics.registerFont(TTFont("BodyBold", heavy))
            font, bold, rupee = "Body", "BodyBold", "₹"
        except Exception:
            pass
    money = lambda n: inr(n).replace("₹", rupee)
    rate = lambda t: _rate(t).replace("₹", rupee)

    by_group = doc.get("level") == "group"
    items, negative, left_out = _stock_rows(doc)
    navy, red, dim = colors.HexColor(NAVY), colors.HexColor(RED), colors.HexColor(DIM)
    small = ParagraphStyle("small", fontName=font, fontSize=8.5, leading=10.5)
    head_style = ParagraphStyle("h", fontName=bold, fontSize=16, leading=20, textColor=colors.HexColor(INK))
    sub = ParagraphStyle("s", fontName=font, fontSize=9.5, leading=13, textColor=dim)
    section = ParagraphStyle("sec", fontName=bold, fontSize=11, leading=15, spaceBefore=8, spaceAfter=3)

    if by_group:
        header, widths, right_from = ["Stock group", "Quantity", "Value"], [100 * mm, 35 * mm, 45 * mm], 1
        cells = lambda r: [Paragraph(e(r.get("name") or ""), small), r.get("qty_text") or "", money(r.get("value"))]
    else:
        header, widths, right_from = ["Item", "Category", "Quantity", "Rate", "Value"], \
            [78 * mm, 22 * mm, 22 * mm, 28 * mm, 30 * mm], 2
        cells = lambda r: [Paragraph(e(r.get("name") or ""), small), Paragraph(e(r.get("group") or ""), small),
                           r.get("qty_text") or "", rate(r.get("rate_text")), money(r.get("value"))]

    def table(rows, with_total=False):
        data = [header] + [cells(r) for r in rows]
        if with_total:
            data.append(["Total"] + [""] * (len(header) - 2) + [money(doc.get("total_value") or 0)])
        t = Table(data, colWidths=widths, repeatRows=1)
        style = [("FONT", (0, 0), (-1, -1), font, 8.5), ("FONT", (0, 0), (-1, 0), bold, 8.5),
                 ("BACKGROUND", (0, 0), (-1, 0), navy), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                 ("ALIGN", (right_from, 0), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                 ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor(LINE)),
                 ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
        for i, r in enumerate(rows, start=1):
            if i % 2 == 0:
                style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor(ZEBRA)))
            if r in negative:
                style.append(("TEXTCOLOR", (right_from, i), (-1, i), red))
        if with_total:
            style += [("FONT", (0, -1), (-1, -1), bold, 9), ("LINEABOVE", (0, -1), (-1, -1), 1, navy)]
        t.setStyle(TableStyle(style))
        return t

    when = ""
    try:
        when = datetime.datetime.fromisoformat(doc["as_of"]).astimezone(IST).strftime("%d %b %Y, %I:%M %p")
    except (KeyError, TypeError, ValueError):
        pass
    noun = "group" if by_group else "item"
    story = [Paragraph("R. S. Infotech – Stock Summary", head_style),
             Paragraph(f"As on {today:%A, %d %b %Y}" + (f" &middot; read from Tally {when}" if when else ""), sub),
             Spacer(1, 4),
             Paragraph(f"<b>Stock value {money(doc.get('total_value') or 0)}</b> &middot; {len(items)} {noun}s in stock"
                       f" &middot; {len(negative)} below zero"
                       + (f" &middot; {left_out} with no stock or no value not listed" if left_out else ""), sub)]
    if doc.get("matches") is False:
        story.append(Paragraph(f"Tally's P&amp;L closing stock is {money(doc.get('pl_closing_stock'))} -- check in Tally.",
                               ParagraphStyle("w", parent=sub, textColor=red)))
    if negative:
        story += [Paragraph("Below zero – check these in Tally", ParagraphStyle("n", parent=section, textColor=red)),
                  table(negative)]
    story += [Paragraph(f"All {noun}s in stock", section), table(items, with_total=True)]

    def footer(canvas, d):
        canvas.saveState()
        canvas.setFont(font, 7.5)
        canvas.setFillColor(dim)
        canvas.drawString(15 * mm, 10 * mm, f"R. S. Infotech – Stock Summary as on {today:%d %b %Y}")
        canvas.drawRightString(A4[0] - 15 * mm, 10 * mm, f"Page {d.page}")
        canvas.restoreState()

    out = io.BytesIO()
    SimpleDocTemplate(out, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm,
                      bottomMargin=16 * mm, title=f"Stock Summary {today:%d %b %Y}",
                      author="R. S. Infotech").build(story, onFirstPage=footer, onLaterPages=footer)
    return out.getvalue()


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
}
DAILY = ["cash", "bank", "purchase", "sales"]
WEEKLY = ["challans", "proformas", "stock"]
GROUPS = {"daily": DAILY, "weekly": WEEKLY, "all": DAILY}


def build_email(kind, day, report):
    """(subject, plain text, html) for one report and day. For a daily
    report, report is the day's daily_reports document, or None when that
    day was never synced; for a pending list it's the list (None when it
    was never synced) and day is the day it's sent. The subject is the
    report's name and the date, and only that."""
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
               "The Tally PC may have been off or Tally closed at the 10 AM sync.")
        return subject, f"R. S. Infotech -- {name} for {label}\n\n{msg}\n\n{DASHBOARD_URL}", _wrap(name, label, f"<p>{e(msg)}</p>")
    lines, body = build(report)
    text = "\n".join([f"R. S. Infotech -- {name} for {label}", ""] + lines + ["", DASHBOARD_URL])
    return subject, text, _wrap(name, label, body)


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


def read_pending(kind):
    """The pending list as the sync last left it, or None if it never ran.
    Delivery Challans are one document each; the Proforma list is one
    document holding them all (sync_tally.py, push_pending_proformas), and
    the stock one document too (sync_tally.py, _sync_stock)."""
    if kind == "challans":
        return [d.to_dict() for d in _db().collection(CHALLAN_COLLECTION).stream()]
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
    # morning, and each failure is an email from GitHub; say what's missing
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
    day = datetime.date.fromisoformat(dates[0]) if dates else today - datetime.timedelta(days=1)
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
        else:
            subject, text, html_body = build_email(kind, day, report)
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
