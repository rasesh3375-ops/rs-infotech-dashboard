#!/usr/bin/env python3
"""
Pulls Daily Sales, Proforma Invoices, Daily Purchase, Daily Profit & Loss
and Daily Cash Vouchers out of a local Tally Prime install (via its HTTP/XML export
gateway) and writes one document per day to Firestore, for the
rs-infotech dashboard (../rs-infotech/index.html) to read. Alongside that
it stores Tally's own Profit & Loss for every week, month, quarter,
half-year and financial year the synced days fall in, and the open
Delivery Notes.

Stock is handled with care. Computing closing stock balances and values
as of a date was consistently the slowest thing Tally did, and a Stock
Summary export once coincided with a real Tally Prime crash (Memory
Access Violation) on shared company data. So the per-day sync never asks
for it: the stock tile is Tally's own Stock Summary report by stock group,
read at most every STOCK_MIN_HOURS, one attempt each, a time limit, and a
pause of STOCK_PAUSE_HOURS after Tally fails to answer in time -- see
_sync_stock.

Run this on the SAME PC as Tally Prime, with Tally open and the company
loaded. See the setup walkthrough for how to enable Tally's XML gateway,
create the Firebase project, and schedule this script.

Usage:
  python sync_tally.py                  # syncs today
  python sync_tally.py --date 2026-09-10
  python sync_tally.py --backfill-from 2026-04-01              # syncs every day from then to today, in one run
  python sync_tally.py --backfill-from 2026-04-01 --backfill-to 2026-06-30
  python sync_tally.py --dry-run         # prints what would be written, does not touch Firestore
  python sync_tally.py --verbose         # prints each Tally request/response summary
  python sync_tally.py --dump-raw-dir ./raw   # saves every raw Tally XML response (debugging)

Exit code is non-zero if the sync failed outright (Tally unreachable, or
Firestore write failed). A report that came back empty or a P&L that could
not be confidently parsed is NOT a failure -- it is written with a flag so
the dashboard can say so, rather than making the whole day's sync fail
because one report was odd.
"""

import argparse
import contextlib
import datetime
import json
import logging
import logging.handlers
import os
import platform
import re
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import requests

# ---------------------------------------------------------------------------
# Config -- the only things you should need to change to point this at a
# different Tally company or Firebase project.
# ---------------------------------------------------------------------------

TALLY_URL = "http://localhost:9000"

# Must match the company name exactly as it appears in Tally Prime's company
# list (Gateway of Tally, top left). Case and spacing matter to Tally.
TALLY_COMPANY_NAME = "R. S. Infotech"

# Path to the Firebase service-account JSON key you download from
# Firebase Console -> Project settings -> Service accounts -> Generate new
# private key. NEVER commit this file -- it is gitignored.
SERVICE_ACCOUNT_PATH = os.path.join(os.path.dirname(__file__), "service-account.json")

FIRESTORE_COLLECTION = "daily_reports"

# Every Delivery Note Tally has, upserted here regardless of date -- see
# _delivery_challan_records and push_delivery_challans_to_firestore below.
# Unlike FIRESTORE_COLLECTION this is not one document per day; it's one
# document per voucher, kept in sync with Tally on every run.
DELIVERY_CHALLAN_COLLECTION = "delivery_challans"

# Tally's own P&L for each whole week / month / quarter / half-year /
# financial year, one document per period, keyed "<period>_<start date>"
# (e.g. "monthly_2026-09-01") -- see sync_period_reports. The dashboard's
# Weekly..Yearly tabs read these instead of adding days up.
PERIOD_COLLECTION = "period_reports"
PERIOD_NAMES = ("weekly", "monthly", "quarterly", "half_yearly", "yearly")

# Every Proforma Invoice Tally still has, whatever its date, as one document
# -- see _pending_proforma_records and push_pending_proformas. It lives in
# PERIOD_COLLECTION only because the Firestore rules already let the
# dashboard read that collection; a collection of its own would need the
# rules edited by hand in the Firebase console first.
PENDING_PROFORMA_DOC = "pending_proforma_invoices"

# Every party's balance under Sundry Debtors and Sundry Creditors, as of the
# latest sync, as one document -- see fetch_party_balances. Kept in
# PERIOD_COLLECTION for the same reason as the Proforma list.
PARTY_BALANCES_DOC = "sundry_balances"

# Each stock group's closing quantity and value, and their total, as of the
# latest read, as one document -- see _sync_stock for how carefully that's
# done.
STOCK_DOC = "stock_summary"

# Serial numbers, for the dashboard's Serial No. Search: every stock line on
# a bill whose item has a description typed under it in Tally -- which is
# where this company's accounts staff type the serial number (the owner, on
# 9 Oct 2026). One document per month, "serials_2026-04" and so on, plus
# SERIALS_DOC listing the months, so the dashboard loads a dozen small
# documents rather than one that would outgrow Firestore's 1 MB limit in a
# few years. In PERIOD_COLLECTION for the same reason as the Proforma list.
SERIALS_DOC = "serials_index"
SERIALS_MONTH_PREFIX = "serials_"
SERIAL_TEXT_MAX = 600
STOCK_MIN_HOURS = 3
# Off from 5 Oct 2026 while Tally kept hanging, when the stock came from a
# StockItem collection with every item's closing value -- the same kind of
# per-object request as the ledger balances that turned out to be the cause.
# Back on the same day with Tally's own Stock Summary report instead: by
# hand on the laptop it answered in 0.2 s plain and 0.1 s exploded, and its
# total matched the P&L closing stock to the paisa (Rs.3,53,782.02).
STOCK_ENABLED = True
# Balances -- cash and bank opening/closing for the daily emails, and every
# party's balance for the Sundry Debtors/Creditors tiles -- come from
# Tally's own Group Summary report, one group at a time (fetch_group_summary).
# On 5 Oct 2026 every per-ledger balance request hung Tally: a Ledger
# collection with CLOSINGBALANCE, and then one with only OPENINGBALANCE,
# which in this company Tally also works out from the earlier years. Python
# giving up after its time limit doesn't help -- Tally carries on with the
# request nobody is waiting for. The same morning the Group Summary answered
# in 0.1 s for Bank Accounts, 1.6 s for Sundry Creditors (70 lines) and
# 5.3 s for Sundry Debtors (154 lines), like the Profit and Loss report,
# which has always answered in under a second.
#
# Even so, it's asked for sparingly: cash and bank only for days this
# recent (the daily emails only ever need yesterday's), debtors and
# creditors at most every PARTY_BALANCES_EVERY_MINUTES outside the daily
# run, one attempt each, and after Tally fails to answer one in time, none
# is asked for again for BALANCES_PAUSE_HOURS.
BALANCE_DAYS = 8
# A bill this old or older counts towards a party's "over_60" -- the figure
# the weekly Debtors Pending 60+ Days email is about.
OVERDUE_REPORT_DAYS = 60
GROUP_SUMMARY_TIMEOUT_SECONDS = 30
PARTY_BALANCES_EVERY_MINUTES = 60
BALANCES_PAUSE_HOURS = 12
BALANCES_PAUSE_FILE = "balances_paused_until.txt"
PARTY_BALANCES_MARKER = "party_balances_at.txt"
# Voucher base types that post to the books, for the check that a day's
# opening + its entries = its closing. Orders, delivery and receipt notes,
# stock journals, memorandum and reversing journals don't post, and neither
# does an optional or cancelled voucher of any type.
ACCOUNTING_BASES = {"sales", "purchase", "payment", "receipt", "contra", "journal", "credit note", "debit note"}
STOCK_PAUSE_HOURS = 12
STOCK_TIMEOUT_SECONDS = 60

# The dashboard's Sync now button and this PC's answer to it -- see
# run_listener. "request" is the one document the dashboard may write
# (the Firestore rules allow nothing else); "status" is written only here.
SYNC_CONTROL_COLLECTION = "sync_control"

# The listener also syncs today once an hour between these hours (inclusive,
# this PC's clock), so today's tiles are never much more than an hour old.
HOURLY_SYNC_FROM_HOUR = 10
HOURLY_SYNC_TO_HOUR = 20

# Near-live: every CHANGE_POLL_SECONDS the listener asks Tally for the
# company's alteration counters, which go up whenever a voucher or master
# is saved, and syncs today once they've stopped moving -- so the
# dashboard is a few minutes behind Tally instead of up to an hour. See
# _TallyChangeWatch for why it asks Tally rather than watching its files.
CHANGE_POLL_SECONDS = 120
CHANGE_QUIET_SECONDS = 60          # wait for entries to stop changing
CHANGE_MIN_GAP_MINUTES = 5         # and never sync more often than this

# Two PCs can run the sync: the main one (sync_role.txt says "primary";
# Setup-Tally-Sync.cmd writes it on the accounts PC) and a backup (no file
# -- the owner's laptop). The backup only syncs while the main one hasn't
# checked in for AGENT_STALE_MINUTES, so the two never both sync.
AGENT_STALE_MINUTES = 15

REQUEST_TIMEOUT_SECONDS = 120

log = logging.getLogger("tally_sync")


# ---------------------------------------------------------------------------
# Low-level Tally HTTP/XML plumbing
# ---------------------------------------------------------------------------

def _host_name():
    """This PC's name, for the status and agent documents: the network name,
    the same value platform.node() gives, so the PC keeps its name on the
    dashboard. Not platform.node() itself: on Windows that can run
    "cmd /c ver" to find the Windows version, and from the window-less
    listener every such cmd opened a Command Prompt window in front of
    whatever the owner was doing -- once at every sync."""
    return socket.gethostname()


if os.name == "nt":
    # The same "cmd /c ver" behind any other platform call, here or in a
    # library: the Windows version is never needed, and asking for it must
    # not open a window.
    platform._syscmd_ver = lambda system="", release="", version="", *a, **k: (system, release, version)


class TallyError(RuntimeError):
    pass


class TallyTimeout(TallyError):
    """Tally was reachable but didn't answer in time -- it may still be
    working on the request, so the balance reads pause after one of these
    (see _GroupBalances). Tally being closed is a plain TallyError."""


def _sanitize_xml_text(raw_bytes):
    """Tally's HTTP responses routinely contain illegal control characters
    (Tally uses some low ASCII bytes internally as field separators in a
    few report exports) that make xml.etree choke with
    'not well-formed (invalid token)'. Strip anything that isn't a valid
    XML character, and escape any stray bare '&' that isn't already part
    of an entity -- Tally does not always escape ledger/party names that
    contain '&'.
    """
    text = raw_bytes.decode("utf-8", errors="replace")
    # Valid XML 1.0 chars: #x9 | #xA | #xD | [#x20-#xD7FF] | ...
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", text)
    text = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9a-fA-F]+;)", "&amp;", text)
    # Tally also emits *escaped* numeric character references (e.g. "&#4;")
    # for the same internal separator bytes -- those pass the raw-byte strip
    # above untouched (they're plain ASCII "&#4;") but still point at a
    # codepoint XML 1.0 forbids, so expat rejects them at parse time with
    # "reference to invalid character number". Drop only the ones that are
    # actually invalid; leave legitimate references (e.g. "&#8377;" for a
    # currency symbol) alone.
    def _drop_invalid_char_ref(m):
        codepoint = int(m.group("hex"), 16) if m.group("hex") is not None else int(m.group("dec"))
        if codepoint in (0x9, 0xA, 0xD) or 0x20 <= codepoint <= 0xD7FF or 0xE000 <= codepoint <= 0xFFFD or 0x10000 <= codepoint <= 0x10FFFF:
            return m.group(0)
        return ""
    text = re.sub(r"&#x(?P<hex>[0-9a-fA-F]+);|&#(?P<dec>\d+);", _drop_invalid_char_ref, text)
    # Tally emits tag/attribute names with a bare colon in them (seen in
    # multi-language name variants, e.g. "<LANGUAGENAME:1033>") which expat
    # reads as an XML namespace prefix that was never declared, failing with
    # "unbound prefix". Tally means nothing by the colon -- it's not really
    # namespacing anything -- so replace it with an underscore, but only
    # inside tag/attribute name position (within "<...>" delimiters, and
    # never inside a quoted attribute value), so a colon that's legitimately
    # part of a ledger name, time value, etc. in element *content* is left
    # completely alone.
    def _fix_unbound_prefixes(tag_match):
        tag = tag_match.group(0)
        parts = re.split(r'("[^"]*"|\'[^\']*\')', tag)
        for i in range(0, len(parts), 2):
            parts[i] = re.sub(r"([A-Za-z_][\w.]*):(?=[\w.])", r"\1_", parts[i])
        return "".join(parts)
    text = re.sub(r"<[^>]+>", _fix_unbound_prefixes, text)
    return text


def _post_xml(xml_request, dump_raw_dir=None, dump_name=None, timeout=None, max_attempts=3):
    """Tally's HTTP/XML gateway has been observed to stall for minutes with
    no apparent cause -- CPU, memory and disk all idle -- and then answer
    the very same request in seconds a moment later. That is a transient
    hiccup in Tally itself, not a reason to give up, so retry network-level
    failures (timeout / connection refused) a couple of times with a short
    pause before actually raising. A malformed response or a real error
    from Tally is NOT retried -- those are deterministic and retrying just
    wastes the same wait again.
    """
    timeout = timeout or REQUEST_TIMEOUT_SECONDS
    resp = None
    last_error = None
    # Every request is logged as it goes out and when it comes back, with
    # how long Tally took, so a log that stops after a "->" line names the
    # exact request Tally froze on. Added after Tally hung twice on 5 Oct
    # 2026 at the start of a sync without saying which request did it.
    m = re.search(r"<ID>(.*?)</ID>|<REPORTNAME>(.*?)</REPORTNAME>", xml_request)
    what = dump_name or (m and (m.group(1) or m.group(2))) or "request"
    to = re.search(r"<SVTODATE>(\d+)</SVTODATE>", xml_request)
    what += f" @{to.group(1)}" if to else ""
    started = time.time()
    log.info("Tally -> %s (pid %d)", what, os.getpid())
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.post(
                TALLY_URL,
                data=xml_request.encode("utf-8"),
                headers={"Content-Type": "text/xml; charset=utf-8"},
                timeout=timeout,
            )
            break
        except requests.exceptions.ConnectionError as e:
            last_error = TallyError(
                f"Could not reach Tally at {TALLY_URL}. Is Tally Prime open, the company "
                f"loaded, and the HTTP/XML gateway enabled (F1 > Settings > Connectivity)? ({e})"
            )
        except requests.exceptions.Timeout:
            last_error = TallyTimeout(f"Tally did not respond within {timeout}s.")
        if attempt < max_attempts:
            log.warning("Attempt %d/%d failed (%s) -- retrying in 5s...", attempt, max_attempts, last_error)
            time.sleep(5)
    if resp is None:
        log.info("Tally <- %s FAILED after %.1f s", what, time.time() - started)
        raise last_error
    log.info("Tally <- %s %.1f s, %d bytes", what, time.time() - started, len(resp.content))

    if resp.status_code != 200:
        raise TallyError(f"Tally returned HTTP {resp.status_code}: {resp.text[:300]}")

    cleaned = _sanitize_xml_text(resp.content)

    if dump_raw_dir and dump_name:
        os.makedirs(dump_raw_dir, exist_ok=True)
        with open(os.path.join(dump_raw_dir, dump_name + ".xml"), "w", encoding="utf-8") as f:
            f.write(cleaned)

    if "<LINEERROR>" in cleaned:
        m = re.search(r"<LINEERROR>(.*?)</LINEERROR>", cleaned, re.DOTALL)
        raise TallyError(f"Tally reported an error: {m.group(1) if m else cleaned[:300]}")

    try:
        return ET.fromstring(cleaned)
    except ET.ParseError as e:
        raise TallyError(f"Could not parse Tally's XML response ({e}). First 500 chars:\n{cleaned[:500]}")


def _fmt_date(d):
    return d.strftime("%Y%m%d")


def _collection_request(collection_name, obj_type, fetch_fields, from_date, to_date, formulae=None, company=None,
                        current_date=None):
    """Builds a TDL Collection export request. This is the reliable, structured
    way to pull voucher/ledger/stock-item data out of Tally -- Tally computes
    the collection from its own object model, rather than us scraping a
    display report.

    SVFROMDATE/SVTODATE alone do NOT restrict a plain Voucher collection to
    that period -- confirmed against real data, where this returned every
    voucher back to the start of the financial year (615 "cash vouchers"
    and hundreds of "purchase" vouchers for a single day whose own Day Book
    showed 9 vouchers total).

    A first attempt at fixing this added a `$Date = ##SVFROMDATE` TDL
    filter formula -- that turned out to not work either: it silently
    matched nothing at all, for every voucher, every day, so results
    looked plausible on days that genuinely had zero purchase/cash
    vouchers and were simply wrong (missing real data) on a day that had
    real sales vouchers. Rather than keep guessing at unverified TDL
    filter syntax, date filtering for vouchers is done in Python instead
    (see fetch_vouchers_for_date), against the DATE field Tally already
    returns for every voucher record -- no Tally-side date comparison to
    get subtly wrong.
    """
    fetch_xml = "".join(f"<FETCH>{f}</FETCH>" for f in fetch_fields)
    current_xml = f"\n    <SVCURRENTDATE>{_fmt_date(current_date)}</SVCURRENTDATE>" if current_date else ""
    formulae_xml = ""
    filter_xml = ""
    if formulae:
        names = []
        for fname, expr in formulae.items():
            formulae_xml += f'<SYSTEM TYPE="Formulae" NAME="{fname}">{expr}</SYSTEM>'
            names.append(fname)
        filter_xml = "".join(f"<FILTER>{n}</FILTER>" for n in names)

    return f"""<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION>
  <TALLYREQUEST>EXPORT</TALLYREQUEST>
  <TYPE>COLLECTION</TYPE>
  <ID>{collection_name}</ID>
 </HEADER>
 <BODY>
  <DESC>
   <STATICVARIABLES>
    <SVCURRENTCOMPANY>{escape(company or TALLY_COMPANY_NAME)}</SVCURRENTCOMPANY>
    <SVFROMDATE>{_fmt_date(from_date)}</SVFROMDATE>
    <SVTODATE>{_fmt_date(to_date)}</SVTODATE>{current_xml}
    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   </STATICVARIABLES>
   <TDL>
    <TDLMESSAGE>
     <COLLECTION NAME="{collection_name}" ISMODIFY="No">
      <TYPE>{obj_type}</TYPE>
      {filter_xml}
      {fetch_xml}
     </COLLECTION>
     {formulae_xml}
    </TDLMESSAGE>
   </TDL>
  </DESC>
 </BODY>
</ENVELOPE>"""


def _collection_records(root, tag):
    """Real records (VOUCHER, LEDGER, ...) live as direct children of the
    <COLLECTION> element under <DATA>. Tally's own CMPINFO header --
    present in every response -- carries same-named counter fields (e.g.
    "<VOUCHER>16</VOUCHER>" meaning 16 voucher types exist, "<LEDGER>59
    </LEDGER>" meaning 59 ledgers exist), so a root.iter(tag) search over
    the whole document matches those counters as if they were real
    records: confirmed against real data, where a Voucher collection that
    Tally itself left completely empty (zero matches) still produced one
    phantom "voucher" with every field blank, because root.iter("VOUCHER")
    found CMPINFO's <VOUCHER>16</VOUCHER> counter instead of nothing.
    """
    collection = root.find(".//DATA/COLLECTION")
    if collection is None:
        return []
    return collection.findall(tag)


def _text(el, tag, default=""):
    child = el.find(tag)
    return child.text.strip() if child is not None and child.text else default


def _num(el, tag, default=0.0):
    raw = _text(el, tag, "")
    if not raw:
        return default
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Report builders
# ---------------------------------------------------------------------------

# Every ledger's group as Tally names it, kept from the ledger list each
# sync already fetches: which Group Summary to ask for a cash or bank
# ledger's balance, and the group shown beside each party.
_LEDGER_PARENTS = {}


def _fetch_ledger_names_under(date, wanted_parents, dump_raw_dir=None, dump_name="ledger_list"):
    """Ledgers parented directly under any of wanted_parents (lowercase).
    Shared by the Cash-in-Hand lookup (below) and the Bank Accounts lookup
    -- if a setup nests these under a sub-group instead, add that
    sub-group's name to the caller's parent set.
    """
    xml_req = _collection_request("LedgerList", "Ledger", ["NAME", "PARENT"], date, date)
    root_el = _post_xml(xml_req, dump_raw_dir, dump_name)
    names = set()
    for led in _collection_records(root_el, "LEDGER"):
        if led.get("NAME") or _text(led, "NAME"):
            _LEDGER_PARENTS[(led.get("NAME") or _text(led, "NAME")).strip()] = _text(led, "PARENT").strip()
        parent = _text(led, "PARENT").strip().lower()
        if parent in wanted_parents:
            name = led.get("NAME") or _text(led, "NAME")
            if name:
                names.add(name.strip())
    return names


def fetch_cash_ledger_names(date, dump_raw_dir=None):
    """Ledgers parented directly under 'Cash-in-Hand' -- Cash, Petty Cash, etc."""
    return _fetch_ledger_names_under(date, {"cash-in-hand"}, dump_raw_dir, "ledger_list")


def fetch_bank_ledger_names(date, dump_raw_dir=None):
    """Ledgers parented under Tally's two standard bank groups -- 'Bank
    Accounts' (current/savings) and 'Bank OD A/c' (overdraft/cash credit)."""
    return _fetch_ledger_names_under(date, {"bank accounts", "bank od a/c"}, dump_raw_dir, "bank_ledger_list")


def fetch_voucher_type_parents(date, dump_raw_dir=None):
    """Maps each Voucher Type's name to its base type (Sales, Purchase,
    Payment, Receipt, Journal, ...), read directly off Tally's own
    classification -- the same PARENT-lookup pattern already used above
    for Ledger -> Cash-in-Hand. This exists because the documented-looking
    $$IsSales/$$IsPurchase TDL system formulae were tried first and
    returned zero matches even for a voucher type ("Tax Invoice") that
    Tally's own Voucher Type Alteration screen confirmed as "Select type
    of voucher: Sales" -- so this reads Tally's actual classification
    directly instead of trusting an unverified function.
    """
    xml_req = _collection_request("VchTypeList", "VoucherType", ["NAME", "PARENT"], date, date)
    root = _post_xml(xml_req, dump_raw_dir, "voucher_types")
    parents = {}
    for vt in _collection_records(root, "VOUCHERTYPE"):
        name = (vt.get("NAME") or _text(vt, "NAME") or "").strip().lower()
        parent = _text(vt, "PARENT").strip().lower()
        if name:
            parents[name] = parent
    return parents


VOUCHER_FETCH_FIELDS = ["DATE", "VOUCHERNUMBER", "PARTYLEDGERNAME", "VOUCHERTYPENAME", "NARRATION",
                        "ISOPTIONAL", "ISCANCELLED", "ALLLEDGERENTRIES.LIST", "ALLINVENTORYENTRIES.LIST"]


def _fetch_all_voucher_records(anchor_date, dump_raw_dir=None):
    """One Tally request for every voucher it's willing to return -- empirically
    the whole financial year, since Tally's Voucher collection ignores
    SVFROMDATE/SVTODATE entirely. Shared by fetch_vouchers_for_date (a single
    day) and fetch_vouchers_grouped_by_date (a backfill across many days), so
    a backfill needs this request only once instead of once per day.
    """
    extra = _serial_fetch_field()
    xml_req = _collection_request(
        "VchList",
        "Voucher",
        VOUCHER_FETCH_FIELDS + ([extra] if extra else []),
        anchor_date,
        anchor_date,
    )
    root = _post_xml(xml_req, dump_raw_dir, "vouchers")
    return _collection_records(root, "VOUCHER")


def fetch_vouchers_grouped_by_date(dump_raw_dir=None):
    """Every voucher Tally has, bucketed by its own DATE field -- the
    backfill path's single voucher fetch, reused for every day in the
    requested range instead of fetching once per day.
    """
    by_date = {}
    for v in _fetch_all_voucher_records(datetime.date.today(), dump_raw_dir):
        by_date.setdefault(_text(v, "DATE"), []).append(v)
    return by_date


def fetch_vouchers_for_date(date, dump_raw_dir=None):
    """Every voucher posted on the given date, unfiltered by class -- the
    sales, purchase and cash-voucher reports are all derived from this one
    fetch (see _filter_by_class and _cash_vouchers_from below) instead of
    each making its own separate request against Tally.
    """
    wanted = _fmt_date(date)
    return [v for v in _fetch_all_voucher_records(date, dump_raw_dir) if _text(v, "DATE") == wanted]


def _voucher_amount(v):
    for entry in v.findall(".//ALLLEDGERENTRIES.LIST"):
        if _text(entry, "ISPARTYLEDGER").lower() == "yes":
            return abs(_num(entry, "AMOUNT"))
    # No entry was flagged as the party ledger -- fall back to the single
    # largest-magnitude entry rather than silently guessing zero.
    amounts = [abs(_num(entry, "AMOUNT")) for entry in v.findall(".//ALLLEDGERENTRIES.LIST")]
    return max(amounts) if amounts else 0.0


def _voucher_description(v):
    """What this voucher was actually for, for display alongside the
    party/amount everywhere a voucher shows up. Prefers the stock items
    involved (what was actually sold/bought), then falls back to the
    narration typed on the voucher, then to whichever ledger(s) besides
    the party were posted to (e.g. "Sales Account" on a plain service
    invoice with no narration and no stock items) -- in roughly that order
    of how likely each is to actually say something useful.
    """
    items = []
    for inv in v.findall(".//ALLINVENTORYENTRIES.LIST"):
        name = (inv.get("NAME") or _text(inv, "STOCKITEMNAME") or "").strip()
        if name and name not in items:
            items.append(name)
    if items:
        shown = ", ".join(items[:4])
        if len(items) > 4:
            shown += f" +{len(items) - 4} more"
        return shown

    narration = _text(v, "NARRATION").strip()
    if narration:
        return narration

    other_ledgers = []
    for entry in v.findall(".//ALLLEDGERENTRIES.LIST"):
        if _text(entry, "ISPARTYLEDGER").lower() == "yes":
            continue
        name = (entry.get("NAME") or _text(entry, "LEDGERNAME") or "").strip()
        if name and name not in other_ledgers:
            other_ledgers.append(name)
    return ", ".join(other_ledgers[:3])


def _delivery_items_summary(v):
    """Item name + quantity for each stock line on a Delivery Note --
    what was actually dispatched is the whole point of a Delivery Challan
    list, unlike _voucher_description above (item names only) which is
    enough for a Sales/Purchase summary. Falls back to the narration if a
    voucher somehow has no inventory entries at all.
    """
    parts = []
    for inv in v.findall(".//ALLINVENTORYENTRIES.LIST"):
        name = (inv.get("NAME") or _text(inv, "STOCKITEMNAME") or "").strip()
        if not name:
            continue
        qty = _text(inv, "ACTUALQTY").strip()
        parts.append(f"{name} ({qty})" if qty else name)
    if parts:
        shown = ", ".join(parts[:4])
        if len(parts) > 4:
            shown += f" +{len(parts) - 4} more"
        return shown
    return _text(v, "NARRATION").strip()


def _tally_date_to_iso(raw):
    """Tally's own DATE field on a voucher comes back as YYYYMMDD (the same
    format _fmt_date builds for requests) -- convert to YYYY-MM-DD to match
    every other date in this file and in Firestore."""
    raw = raw.strip()
    if len(raw) != 8 or not raw.isdigit():
        return raw
    return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"


def _delivery_challan_doc_id(date_iso, voucher_no):
    """Firestore document IDs can't contain '/', and Tally voucher numbers
    routinely do (a "24-25/451" numbering series, for instance) -- replace
    anything that isn't safe in a single path segment so the same voucher
    always lands on the same document across repeated syncs."""
    safe_no = re.sub(r"[^A-Za-z0-9_.-]", "_", voucher_no.strip()) or "unknown"
    return f"{date_iso}_{safe_no}"


def _earliest_voucher_date(vouchers):
    """Earliest voucher date in a fetch, as YYYY-MM-DD -- the start of what
    this run can actually see in Tally (see push_delivery_challans_to_firestore)."""
    dates = [d for d in (_tally_date_to_iso(_text(v, "DATE")) for v in vouchers) if len(d) == 10]
    return min(dates) if dates else "9999-12-31"


def _delivery_challan_records(vouchers, type_parents):
    """Every Delivery Note voucher Tally has, in the shape the dashboard's
    Pending Delivery Challan list expects.

    Tally has no built-in link between a Delivery Note and whatever Sales
    Invoice later bills it -- confirmed against real data, where none of
    this company's Delivery Note vouchers carry a Tracking Number, the only
    mechanism Tally has for that link (see the deleted
    inspect_delivery_notes.py in git history). So "pending" here means
    exactly "still a Delivery Note in Tally": once one is deleted or
    converted to an invoice there, push_delivery_challans_to_firestore
    removes it on the next sync. The dashboard is view only and records
    nothing itself; Tally is the single source.

    Delivery Notes routinely carry no ledger amount at all (goods go out,
    nothing's been billed yet, so there's often nothing to post to a
    ledger) -- showing a near-always-zero Amount column would look broken,
    so this reports items+quantity instead, which is what actually answers
    "what still needs to be billed".
    """
    records = []
    for v in vouchers:
        vch_type = _text(v, "VOUCHERTYPENAME")
        if type_parents.get(vch_type.strip().lower()) != "delivery note":
            continue
        date_iso = _tally_date_to_iso(_text(v, "DATE"))
        voucher_no = _text(v, "VOUCHERNUMBER")
        records.append({
            "doc_id": _delivery_challan_doc_id(date_iso, voucher_no),
            "date": date_iso,
            "voucher_no": voucher_no,
            "party": _text(v, "PARTYLEDGERNAME"),
            "type": vch_type,
            "description": _delivery_items_summary(v),
        })
    return records


def _is_proforma(voucher_type_name):
    """A Proforma Invoice is a quotation, not a sale. Here its voucher type
    ("Proforma Invoice", confirmed on the dashboard's own Sales list for
    3 Oct 2026) has Sales as its parent, so it used to be added into the
    Sales tile with the real Tax Invoices. Matched by name, ignoring case,
    spaces and hyphens, and allowing the common "Performa" spellings, so a
    second proforma type named slightly differently is still caught."""
    letters = re.sub(r"[^a-z]", "", (voucher_type_name or "").lower())
    return any(word in letters for word in ("proforma", "performa", "perfoma"))


def _pending_proforma_records(vouchers):
    """Every Proforma Invoice in this fetch, for the dashboard's Proforma
    Invoice (Pending for Invoice) list. "Pending" means what it means for
    Delivery Challans (see _delivery_challan_records): still a Proforma
    Invoice in Tally. Once it's deleted there, or turned into a Tax Invoice,
    the next sync drops it. Unlike a Delivery Note, a proforma carries its
    amount, so that's kept."""
    records = []
    for v in vouchers:
        vch_type = _text(v, "VOUCHERTYPENAME")
        if not _is_proforma(vch_type):
            continue
        records.append({
            "date": _tally_date_to_iso(_text(v, "DATE")),
            "voucher_no": _text(v, "VOUCHERNUMBER"),
            "party": _text(v, "PARTYLEDGERNAME"),
            "type": vch_type,
            "amount": round(_voucher_amount(v), 2),
            "description": _voucher_description(v),
        })
    records.sort(key=lambda r: (r["date"], r["voucher_no"]))
    return records


# Asking Tally for ALLINVENTORYENTRIES.LIST gives each stock line's item,
# quantity and amount, but not the description lines typed under the item
# -- where the serial number goes. Found on 9 Oct 2026: the first sync with
# the Serial No. Search found none, while Tally showed "Sr.No.5MLLTK4" under
# a Dell laptop on purchase 1587. Tally only sends a sub-list when it is
# named in the request, and how it wants it named isn't something that can
# be tried from anywhere but the Tally PC. So when a sync finds no
# descriptions it asks Tally once with each of these in turn, keeps the
# first one Tally answers with descriptions in SERIAL_FETCH_FILE, and every
# voucher fetch after that includes it -- one request, as before.
SERIAL_FETCH_CANDIDATES = [
    "ALLINVENTORYENTRIES.BASICUSERDESCRIPTION",
    "AllInventoryEntries.BasicUserDescription",
    "ALLINVENTORYENTRIES.LIST.BASICUSERDESCRIPTION.LIST",
    "ALLINVENTORYENTRIES.*",
]
SERIAL_FETCH_FILE = "serials_fetch.txt"


def _serial_fetch_field():
    """The FETCH field that makes Tally send item descriptions on this PC,
    or None -- see SERIAL_FETCH_CANDIDATES."""
    try:
        with open(os.path.join(SCRIPT_DIR, SERIAL_FETCH_FILE), encoding="utf-8") as f:
            line = f.read().strip()
        return line if line in SERIAL_FETCH_CANDIDATES else None
    except OSError:
        return None


def _probe_serial_fetch():
    """Tries SERIAL_FETCH_CANDIDATES (see there) and returns (field, the
    vouchers Tally sent with it) for the first that brings descriptions, or
    (None, None). Tried at most once a day: the file records a day when
    none worked. Stops at the first request Tally doesn't answer in time,
    so a slow Tally isn't asked again and again."""
    path = os.path.join(SCRIPT_DIR, SERIAL_FETCH_FILE)
    today = datetime.date.today().isoformat()
    try:
        with open(path, encoding="utf-8") as f:
            if f.read().strip() == "none " + today:
                return None, None
    except OSError:
        pass
    for field in SERIAL_FETCH_CANDIDATES:
        try:
            root = _post_xml(_collection_request("VchList", "Voucher", VOUCHER_FETCH_FIELDS + [field],
                                                 datetime.date.today(), datetime.date.today()),
                             dump_name="vouchers with " + field, timeout=120, max_attempts=1)
        except TallyError as e:
            log.info("Item descriptions: Tally didn't take %s -- %s", field, e)
            if isinstance(e, TallyTimeout) or "timed out" in str(e).lower():
                break
            continue
        vouchers = _collection_records(root, "VOUCHER")
        if any(_description_texts(inv) for v in vouchers for inv in v.findall(".//ALLINVENTORYENTRIES.LIST")):
            with open(path, "w", encoding="utf-8") as f:
                f.write(field)
            log.info("Item descriptions: Tally sends them when asked for %s -- every sync asks for it now.", field)
            return field, vouchers
        log.info("Item descriptions: none with %s", field)
    with open(path, "w", encoding="utf-8") as f:
        f.write("none " + today)
    return None, None


def _description_texts(inv):
    """The description lines typed under the item on one stock line, in
    whatever nesting Tally sends them (BASICUSERDESCRIPTION, inside a .LIST
    or not)."""
    return [t.text.strip() for t in inv.iter()
            if t.tag.upper().startswith("BASICUSERDESCRIPTION") and t.text and t.text.strip()]


def _serial_lines(vouchers, type_parents):
    """Every stock line on a real bill (not a proforma, an optional or a
    cancelled voucher) with text typed under the item in Tally -- the item
    description lines, where the serial number goes here -- or a batch name
    of its own, which is where some companies keep serials instead. Grouped
    by month: {"2026-04": [line, ...]}. A line has the voucher's date,
    number, type, base type (purchase, sales, credit note, delivery note,
    ...) and party, the item, its quantity and the text."""
    out = {}
    for v in vouchers:
        if _text(v, "ISOPTIONAL").lower() == "yes" or _text(v, "ISCANCELLED").lower() == "yes":
            continue
        vch_type = _text(v, "VOUCHERTYPENAME")
        if _is_proforma(vch_type):
            continue
        date_iso = _tally_date_to_iso(_text(v, "DATE"))
        if len(date_iso) != 10:
            continue
        for inv in v.findall(".//ALLINVENTORYENTRIES.LIST"):
            texts = _description_texts(inv)
            batches = [b for b in (_text(x, "BATCHNAME") for x in inv.findall("BATCHALLOCATIONS.LIST"))
                       if b and b.lower() not in ("primary batch", "any", "not applicable", "end of list")]
            text = " | ".join(dict.fromkeys(texts + batches))
            if not text:
                continue
            out.setdefault(date_iso[:7], []).append({
                "date": date_iso,
                "voucher_no": _text(v, "VOUCHERNUMBER"),
                "type": vch_type,
                "base": type_parents.get(vch_type.strip().lower(), ""),
                "party": _text(v, "PARTYLEDGERNAME"),
                "item": (inv.get("NAME") or _text(inv, "STOCKITEMNAME") or "").strip(),
                "qty": _text(inv, "ACTUALQTY") or _text(inv, "BILLEDQTY"),
                "text": text[:SERIAL_TEXT_MAX],
            })
    for lines in out.values():
        lines.sort(key=lambda r: (r["date"], r["voucher_no"]))
    return out


def push_serial_index(vouchers, type_parents):
    """Writes the Serial No. Search's documents (see SERIALS_DOC): each
    month this fetch covers is replaced whole -- an empty month too, so a
    line taken off a bill in Tally goes from the search -- and months before
    it are left as they are. Tally hands back only the current financial
    year, so from 1 April last year's months stay searchable instead of
    disappearing. Never stops the sync; returns how many lines were
    written, or None if they couldn't be."""
    try:
        by_month = _serial_lines(vouchers, type_parents)
        how = _serial_fetch_field()
        if not by_month and not how:
            how, probed = _probe_serial_fetch()
            if probed:
                vouchers = probed
                by_month = _serial_lines(vouchers, type_parents)
        first = _earliest_voucher_date(vouchers)[:7]
        last = max([datetime.date.today().isoformat()[:7]] + list(by_month))
        db = _firestore_db()
        col = db.collection(PERIOD_COLLECTION)
        before = col.document(SERIALS_DOC).get()
        months = dict(((before.to_dict() or {}).get("months") or {}) if before.exists else {})
        y, m = int(first[:4]), int(first[5:7])
        while f"{y:04d}-{m:02d}" <= last:
            month = f"{y:04d}-{m:02d}"
            lines = by_month.get(month, [])
            col.document(SERIALS_MONTH_PREFIX + month).set({"month": month, "lines": lines})
            if lines:
                months[month] = len(lines)
            else:
                months.pop(month, None)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        col.document(SERIALS_DOC).set({**(((before.to_dict() or {}) if before.exists else {})),
                                       "months": months, "as_of": _utc_now().isoformat(),
                                       "from": min(months) if months else None,
                                       "descriptions": "read" if (how or by_month) else "not given by Tally"})
        written = sum(len(v) for v in by_month.values())
        log.info("Serial No. Search: %d item lines with a description, %d months in all", written, len(months))
        return written
    except Exception as e:
        log.warning("Serial No. Search not updated: %s", e)
        return None


def _old_year_companies():
    """[(name, first day, last day)] of the earlier years' companies open in
    Tally right now. This company keeps each financial year as a company of
    its own -- "R. S. Infotech (2025-2026)", "R. S. Infotech(2023-2024)" --
    and Tally only answers for a company that is open, so this asks which
    are, keeps those named R. S. Infotech with a year in brackets (not the
    Capital or Payroll ones), and reads the year off the name."""
    today = datetime.date.today()
    root = _post_xml(_collection_request("OpenCompanies", "Company", ["NAME"], today, today),
                     dump_name="open companies", timeout=30, max_attempts=1)
    out = []
    for cmp in _collection_records(root, "COMPANY"):
        name = (cmp.get("NAME") or _text(cmp, "NAME") or "").strip()
        letters = re.sub(r"[^a-z]", "", name.lower())
        m = re.search(r"\((\d{4})\s*-\s*(\d{4})\)\s*$", name)
        if not m or not letters.startswith("rsinfotech") or "capital" in letters or "payroll" in letters:
            continue
        start = datetime.date(int(m.group(1)), 4, 1)
        out.append((name, start, datetime.date(start.year + 1, 3, 31)))
    return sorted(out, key=lambda c: c[1])


def run_import_serials():
    """--import-serials: reads the serial numbers off the bills of the
    earlier years' companies (_old_year_companies) into the Serial No.
    Search, once -- asked for on 9 Oct 2026, to search three years back.
    Those years don't change, so this isn't part of the daily sync: open
    the old companies in Tally (F3, Select Company, alongside R. S.
    Infotech), run this, and close them again.

    Each company is asked for its vouchers with the item descriptions
    (SERIAL_FETCH_FIELDS's remembered form), and only if every voucher it
    sends is dated inside that company's own year are its serial lines
    written -- a reply from the wrong company is refused, not stored. The
    months are written like the daily sync writes the current year's, and
    the current year's months are never touched."""
    field = _serial_fetch_field()
    if not field:
        field, _ = _probe_serial_fetch()
    if not field:
        print("Tally doesn't send the item descriptions (where the serial numbers are) -- nothing imported.")
        return 1
    companies = _old_year_companies()
    if not companies:
        print("No earlier year's company is open in Tally. Open them first: F3 (Select Company), pick\n"
              "R. S. Infotech (2025-2026), then F3 again for (2024-2025) and (2023-2024), and run this again.")
        return 1
    type_parents = fetch_voucher_type_parents(datetime.date.today())
    db = _firestore_db()
    col = db.collection(PERIOD_COLLECTION)
    before = col.document(SERIALS_DOC).get()
    index = (before.to_dict() or {}) if before.exists else {}
    months = dict(index.get("months") or {})
    imported = dict(index.get("imported") or {})
    current_from = min((m for m in months), default="9999-99")
    total = 0
    skipped_empty = []
    for name, start, end in companies:
        # The first run, on 9 Oct 2026, got no vouchers at all from any of
        # the three: Tally hands a Voucher collection the vouchers of the
        # Current Period, which was 1-Apr-26 to 31-Mar-27. So the company's
        # own year is asked for -- as the period, then with the current
        # date inside it too -- and if Tally still sends nothing, the
        # owner sets the period in Tally (Alt+F2) and runs this again.
        vouchers, failed = [], None
        for current in (None, end):
            try:
                root = _post_xml(_collection_request("VchList", "Voucher", VOUCHER_FETCH_FIELDS + [field], start, end,
                                                     company=name, current_date=current),
                                 dump_name=f"vouchers of {name}", timeout=300, max_attempts=1)
            except TallyError as e:
                failed = e
                break
            vouchers = _collection_records(root, "VOUCHER")
            if vouchers:
                break
        if failed:
            print(f"{name}: Tally didn't answer -- {failed}")
            continue
        if not vouchers:
            skipped_empty.append((name, start, end))
        dates = [_tally_date_to_iso(_text(v, "DATE")) for v in vouchers]
        outside = [d for d in dates if not (start.isoformat() <= d <= end.isoformat())]
        if not vouchers or outside:
            print(f"{name}: skipped -- Tally sent {len(vouchers)} vouchers, {len(outside)} of them outside "
                  f"{start:%d %b %Y} to {end:%d %b %Y}.")
            continue
        by_month = _serial_lines(vouchers, type_parents)
        count = 0
        for month, lines in sorted(by_month.items()):
            if month >= current_from and month in months and month not in imported.get(name, {}).get("months", []):
                continue   # a month the daily sync keeps -- never overwritten from here
            col.document(SERIALS_MONTH_PREFIX + month).set({"month": month, "lines": lines, "company": name})
            months[month] = len(lines)
            count += len(lines)
        imported[name] = {"from": start.isoformat(), "to": end.isoformat(), "lines": count,
                          "months": sorted(by_month), "at": _utc_now().isoformat()}
        total += count
        print(f"{name}: {len(vouchers)} vouchers, {count} item lines with a serial number imported.")
    col.document(SERIALS_DOC).set({**index, "months": months, "imported": imported,
                                   "from": min(months) if months else None, "as_of": _utc_now().isoformat()})
    print(f"Done: {total} item lines from {len(companies) - len(skipped_empty)} of {len(companies)} earlier year(s).")
    if skipped_empty:
        print("\nTally sent no vouchers for: " + ", ".join(n for n, _, _ in skipped_empty) + ".\n"
              "In Tally press Alt+F2 (Period) and set it to that year, run this again, and repeat for each year:")
        for name, start, end in skipped_empty:
            print(f"    {name}: From {start:%d-%m-%Y} To {end:%d-%m-%Y}")
        print("Afterwards set the period back to the current year (Alt+F2: From 1-4-" + str(_fy_start(datetime.date.today()).year)
              + " To 31-3-" + str(_fy_start(datetime.date.today()).year + 1) + ").")
    else:
        print("You can close those companies in Tally now.")
    return 0


def _filter_by_class(vouchers, type_parents, wanted_parent, proforma=False):
    """Vouchers whose type has wanted_parent as its base type, with their
    bill totals (GST included). proforma=False leaves Proforma Invoices
    out; proforma=True returns only them, whatever their parent."""
    result = []
    total = 0.0
    for v in vouchers:
        vch_type = _text(v, "VOUCHERTYPENAME")
        if _is_proforma(vch_type) != proforma:
            continue
        if not proforma and type_parents.get(vch_type.strip().lower()) != wanted_parent:
            continue
        amount = _voucher_amount(v)
        result.append({
            "voucher_no": _text(v, "VOUCHERNUMBER"),
            "party": _text(v, "PARTYLEDGERNAME"),
            "type": vch_type,
            "amount": round(amount, 2),
            "description": _voucher_description(v),
        })
        total += amount
    return {"total": round(total, 2), "count": len(result), "vouchers": result}


def _ledger_touching_vouchers_from(vouchers, ledger_names, in_label, out_label):
    """Vouchers where any ledger entry hits one of ledger_names -- shared by
    cash and bank voucher detection, which differ only in which ledgers
    they're looking for and what to call money moving in vs out."""
    result = []
    for v in vouchers:
        matched_entry = None
        for entry in v.findall(".//ALLLEDGERENTRIES.LIST"):
            ledger_name = (entry.get("NAME") or _text(entry, "LEDGERNAME") or "").strip()
            if ledger_name in ledger_names:
                matched_entry = entry
                break
        if matched_entry is None:
            continue
        is_debit = _text(matched_entry, "ISDEEMEDPOSITIVE").lower() == "yes"
        result.append({
            "voucher_no": _text(v, "VOUCHERNUMBER"),
            "party": _text(v, "PARTYLEDGERNAME"),
            "type": _text(v, "VOUCHERTYPENAME"),
            # Which cash or bank ledger -- with several bank accounts, the
            # daily bank email says which one the money went through.
            "ledger": (matched_entry.get("NAME") or _text(matched_entry, "LEDGERNAME") or "").strip(),
            "direction": in_label if is_debit else out_label,
            "amount": round(abs(_num(matched_entry, "AMOUNT")), 2),
            "description": _voucher_description(v),
        })
    return {"count": len(result), "vouchers": result}


def _cash_vouchers_from(vouchers, cash_ledgers):
    return _ledger_touching_vouchers_from(vouchers, cash_ledgers, "cash_in", "cash_out")


def _bank_vouchers_from(vouchers, bank_ledgers):
    return _ledger_touching_vouchers_from(vouchers, bank_ledgers, "bank_in", "bank_out")


def fetch_profit_and_loss(date, dump_raw_dir=None, to_date=None):
    """Uses Tally's own native Profit & Loss report export for a single day
    (SVFROMDATE == SVTODATE == date), or for date..to_date when to_date is
    given -- the same report Tally shows for that period, so Tally does the
    Income/Expense classification and the stock valuation itself instead of
    us reimplementing it. Over a range, Opening Stock is the stock at the
    start of the range and Closing Stock the stock at its end, so the
    net_profit_loss formula below is Tally's period profit as-is. The report's
    display XML is version-dependent, so this looks for the 'Nett Profit'/
    'Nett Loss' line by name rather than a fixed tag position. If it can't
    find one confidently, it returns needs_review=True with the raw line
    items attached instead of guessing a number.
    """
    xml_req = f"""<ENVELOPE>
 <HEADER>
  <TALLYREQUEST>Export Data</TALLYREQUEST>
 </HEADER>
 <BODY>
  <EXPORTDATA>
   <REQUESTDESC>
    <REPORTNAME>Profit and Loss</REPORTNAME>
    <STATICVARIABLES>
     <SVCURRENTCOMPANY>{TALLY_COMPANY_NAME}</SVCURRENTCOMPANY>
     <SVFROMDATE>{_fmt_date(date)}</SVFROMDATE>
     <SVTODATE>{_fmt_date(to_date or date)}</SVTODATE>
    </STATICVARIABLES>
   </REQUESTDESC>
  </EXPORTDATA>
 </BODY>
</ENVELOPE>"""
    root = _post_xml(xml_req, dump_raw_dir, "profit_and_loss")

    # Confirmed against a real response: Tally does NOT include a "Nett
    # Profit"/"Nett Loss" line in this export at all -- it returns a flat,
    # ordered sequence of <DSPACCNAME><DSPDISPNAME>Group Name</DSPDISPNAME>
    # </DSPACCNAME> elements, each immediately followed by a *sibling*
    # <PLAMT><BSMAINAMT>amount</BSMAINAMT></PLAMT> -- not nested together,
    # and a group with nothing posted that day is omitted entirely rather
    # than shown as zero. These group names are Tally's own fixed, built-in
    # primary groups (not user-renameable), so classifying by exact name is
    # as reliable as classification gets without reimplementing Tally's own
    # ledger-to-group resolution.
    #
    # Opening/Closing Stock belong in a single day's Nett Profit -- Tally's
    # own formula is Nett Profit = (Sales+DirectIncome+IndirectIncome+
    # ClosingStock) - (Purchase+DirectExpense+IndirectExpense+OpeningStock),
    # confirmed against a real day (4-Sep-26) to the rupee. But they are
    # BALANCES (stock value at a point in time), not flows like Sales or
    # Purchase -- summing many days' Opening/Closing Stock the way flow
    # figures are legitimately summed across a period does NOT recover the
    # period's real stock movement, it just adds the same large balance
    # figure over and over. Confirmed the hard way: folding stock into
    # total_income/total_expense (which the dashboard sums across a
    # period) inflated a 168-day total by roughly 10x versus Tally's own
    # period report. So stock is tracked in its own fields here, used only
    # for this report's own net_profit_loss, and deliberately kept OUT of
    # total_income/total_expense, which stay flow-only and safe to sum
    # across any number of days. A period's real profit is never a sum of
    # days: it's this same report requested for the whole period -- see
    # sync_period_reports.
    INCOME_GROUPS = {"sales accounts", "direct incomes", "indirect incomes"}
    EXPENSE_GROUPS = {"purchase accounts", "direct expenses", "indirect expenses"}
    STOCK_GROUPS = {"closing stock": "closing", "opening stock": "opening"}
    # Subtotal/heading lines Tally prints alongside the real ones above --
    # safe to skip, not a sign of a missing group like an unrecognized name
    # with a real amount would be.
    KNOWN_SUBTOTAL_LABELS = {"cost of sales :", "gross profit c/o", "gross profit b/f"}

    # Confirmed against a real 11-Sep-26 response: when Tally shows a Cost of
    # Sales sub-schedule (Opening Stock/Purchase Accounts/Closing Stock/Direct
    # Expenses grouped together -- this only appears on some days, which is
    # why 4-Sep parsed fine without this), it renames two of those lines to
    # "Add: Purchase Accounts" and "Less: Closing Stock". Neither matched our
    # plain group names, so both were silently dropped from the total -- the
    # whole point of matched_any is to catch a report matching nothing at
    # all, and it didn't catch this because Sales/Direct Expenses/etc still
    # matched fine, just with two real amounts missing from the sum.
    def _normalized_group_key(name):
        key = name.strip().lower()
        for prefix in ("add: ", "add:", "less: ", "less:"):
            if key.startswith(prefix):
                return key[len(prefix):].strip()
        return key

    # Tally's own Sales Accounts and Purchase Accounts lines, kept apart as
    # well as added into the totals: they are the sales and purchases before
    # GST, exactly as Tally reports them, which is what the Sales and
    # Purchase tiles show. Adding up the invoices can't give that figure --
    # an invoice's total includes GST, and that was why the Sales tile read
    # Rs.32,23,396 for 1 Sep 2026 against Tally's Rs.27,30,991.
    BOOKS_GROUPS = {"sales accounts": "sales_accounts", "purchase accounts": "purchase_accounts"}
    books = {"sales_accounts": 0.0, "purchase_accounts": 0.0}

    total_income = 0.0
    total_expense = 0.0
    opening_stock = 0.0
    closing_stock = 0.0
    matched_any = False
    saw_any_line = False
    pending_name = None
    for child in root:
        if child.tag == "DSPACCNAME":
            disp = child.find("DSPDISPNAME")
            pending_name = "".join(disp.itertext()).strip() if disp is not None else ""
            saw_any_line = True
        elif child.tag == "PLAMT" and pending_name is not None:
            amount = _num(child, "BSMAINAMT") or _num(child, "PLSUBAMT")
            # Also confirmed against that same 11-Sep response: every line
            # inside that Cost of Sales sub-schedule comes back NEGATIVE --
            # including Closing Stock, which should increase profit, not
            # reduce it. These per-line signs don't encode debit/credit
            # individually; Tally only gets the sign right on the subtotal
            # it prints itself. Our own formula below already applies the
            # correct +/- for each group, so every group needs its true
            # magnitude here, not Tally's display sign for that line.
            amount = abs(amount)
            key = _normalized_group_key(pending_name)
            if key in BOOKS_GROUPS:
                books[BOOKS_GROUPS[key]] += amount
            if key in INCOME_GROUPS:
                total_income += amount
                matched_any = True
            elif key in EXPENSE_GROUPS:
                total_expense += amount
                matched_any = True
            elif key in STOCK_GROUPS:
                if STOCK_GROUPS[key] == "closing":
                    closing_stock += amount
                else:
                    opening_stock += amount
                matched_any = True
            elif amount != 0 and key not in KNOWN_SUBTOTAL_LABELS:
                # Anything else with a real amount attached and a name we
                # don't recognise as one of Tally's own subtotal/schedule
                # headings is a line we're silently dropping from the total
                # -- exactly how "Add: Purchase Accounts" and "Less: Closing
                # Stock" went missing before this was added. Surfaced as a
                # warning (visible with --verbose) rather than failing the
                # sync, since one odd line on one day shouldn't block every
                # other day's report.
                log.warning(
                    "%s: unrecognized P&L line %r (Rs.%s) -- not counted in any "
                    "total. If this is a real Income/Expense/Stock line under a "
                    "name not seen before, it needs adding to the matching above.",
                    date, pending_name, amount,
                )
            pending_name = None

    if not saw_any_line:
        # Confirmed against the comment above: Tally omits a group's line
        # entirely when nothing was posted to it that day, so a day with
        # literally nothing posted anywhere in the P&L comes back as an
        # empty sequence of DSPACCNAME/PLAMT pairs -- zero of them, not a
        # zero amount against each one. That used to fall through to the
        # "couldn't find any groups" branch below and get flagged
        # needs_review, which is wrong: it's not that the report was
        # unrecognizable, it's that Tally is correctly saying nothing
        # happened. A genuine quiet day (holiday, Sunday, shop closed)
        # should show Rs.0, not a review flag.
        return {
            "needs_review": False,
            "net_profit_loss": 0.0,
            "total_income": 0.0,
            "total_expense": 0.0,
            "opening_stock": 0.0,
            "closing_stock": 0.0,
            "sales_accounts": 0.0,
            "purchase_accounts": 0.0,
        }

    if not matched_any:
        reason = ("Could not find any of Tally's standard Income/Expense groups (Sales "
                   "Accounts, Direct/Indirect Incomes, Purchase Accounts, Direct/Indirect "
                   "Expenses) in the P&L export.")
        if not dump_raw_dir:
            reason += " Re-run with --dump-raw-dir to save the raw XML for inspection."
        return {"needs_review": True, "reason": reason, "net_profit_loss": None}

    net_profit_loss = (total_income + closing_stock) - (total_expense + opening_stock)

    return {
        "needs_review": False,
        "net_profit_loss": round(net_profit_loss, 2),
        "total_income": round(total_income, 2),
        "total_expense": round(total_expense, 2),
        "opening_stock": round(opening_stock, 2),
        "closing_stock": round(closing_stock, 2),
        "sales_accounts": round(books["sales_accounts"], 2),
        "purchase_accounts": round(books["purchase_accounts"], 2),
    }


# ---------------------------------------------------------------------------
# Firestore
# ---------------------------------------------------------------------------

def _firestore_db():
    import firebase_admin
    from firebase_admin import credentials, firestore

    if not os.path.exists(SERVICE_ACCOUNT_PATH):
        raise RuntimeError(
            f"Service account key not found at {SERVICE_ACCOUNT_PATH}. "
            "Download it from Firebase Console > Project settings > Service accounts."
        )

    if not firebase_admin._apps:
        cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
        firebase_admin.initialize_app(cred)

    return firestore.client()


def push_period_report(doc_id, doc):
    _firestore_db().collection(PERIOD_COLLECTION).document(doc_id).set(doc)


# A day's Profit & Loss is kept apart from the rest of the day, in a
# collection of its own: on 9 Oct 2026 the owner asked for staff logins that
# see everything except P&L, and the database can only allow or refuse a
# whole document, not a field of one. The day's document keeps "books" --
# Tally's Sales and Purchase Accounts, the before-GST figures the Sales and
# Purchase tiles show -- which staff do see.
PL_COLLECTION = "daily_pl"
PL_MOVED_FILE = "pl_moved.txt"


def _split_pl(payload):
    """(the day without its P&L, the P&L) -- see PL_COLLECTION."""
    pl = payload.get("profit_and_loss")
    day = {k: v for k, v in payload.items() if k != "profit_and_loss"}
    if isinstance(pl, dict) and isinstance(pl.get("sales_accounts"), (int, float)):
        day["books"] = {"sales_accounts": pl["sales_accounts"], "purchase_accounts": pl.get("purchase_accounts") or 0}
    return day, pl


def push_to_firestore(date_iso, payload):
    db = _firestore_db()
    day, pl = _split_pl(payload)
    if pl is not None:
        db.collection(PL_COLLECTION).document(date_iso).set(
            {"date": date_iso, "synced_at": payload.get("synced_at"), "profit_and_loss": pl})
    db.collection(FIRESTORE_COLLECTION).document(date_iso).set(day)


def _move_pl_out_of_daily_reports():
    """Once on each PC (PL_MOVED_FILE): every daily_reports document written
    before PL_COLLECTION existed still holds its day's P&L, which staff
    could read. Each one's P&L is copied to PL_COLLECTION and taken off it,
    in batches, and sync_control/status notes when it was done -- the
    dashboard's Users page waits for that before it lets staff be added.
    Harmless to repeat: a document with no P&L left is skipped."""
    from firebase_admin import firestore
    marker = os.path.join(SCRIPT_DIR, PL_MOVED_FILE)
    if os.path.exists(marker):
        return
    db = _firestore_db()
    batch, pending, moved = db.batch(), 0, 0
    for snap in db.collection(FIRESTORE_COLLECTION).stream():
        doc = snap.to_dict() or {}
        if "profit_and_loss" not in doc:
            continue
        day, pl = _split_pl(doc)
        batch.set(db.collection(PL_COLLECTION).document(snap.id),
                  {"date": snap.id, "synced_at": doc.get("synced_at"), "profit_and_loss": pl})
        update = {"profit_and_loss": firestore.DELETE_FIELD}
        if "books" in day:
            update["books"] = day["books"]
        batch.update(snap.reference, update)
        pending, moved = pending + 2, moved + 1
        if pending >= 400:
            batch.commit()
            batch, pending = db.batch(), 0
    if pending:
        batch.commit()
    db.collection(SYNC_CONTROL_COLLECTION).document("status").set({"pl_split_at": _utc_now().isoformat()}, merge=True)
    with open(marker, "w", encoding="utf-8") as f:
        f.write(_utc_now().isoformat())
    log.info("P&L moved out of %d daily documents into %s.", moved, PL_COLLECTION)


def push_party_balances(balances):
    _firestore_db().collection(PERIOD_COLLECTION).document(PARTY_BALANCES_DOC).set(
        {**balances, "as_of": datetime.datetime.now(datetime.timezone.utc).isoformat()})


def fetch_group_summary(group, upto, dump_raw_dir=None, timeout=GROUP_SUMMARY_TIMEOUT_SECONDS):
    """Tally's own Group Summary report for one group, as at upto: each line
    under it (a ledger, or a sub-group as one line) with its closing balance,
    debit-positive. A report rather than a Ledger collection because on
    5 Oct 2026 every per-ledger balance request hung Tally -- CLOSINGBALANCE
    and then OPENINGBALANCE, which in this company Tally also works out
    from earlier years -- while its own Profit and Loss report answered in
    under half a second all along. One attempt only: a retry asks a stuck
    Tally the same thing twice.

    The export is a flat run of <DSPACCNAME><DSPDISPNAME>name</DSPDISPNAME>
    </DSPACCNAME> each followed by a sibling <DSPACCINFO> holding
    DSPCLDRAMT/DSPCLDRAMTA (debit, negative) and DSPCLCRAMT/DSPCLCRAMTA
    (credit), the same layout as the Trial Balance export."""
    from xml.sax.saxutils import escape
    xml_req = f"""<ENVELOPE>
 <HEADER>
  <TALLYREQUEST>Export Data</TALLYREQUEST>
 </HEADER>
 <BODY>
  <EXPORTDATA>
   <REQUESTDESC>
    <REPORTNAME>Group Summary</REPORTNAME>
    <STATICVARIABLES>
     <SVCURRENTCOMPANY>{escape(TALLY_COMPANY_NAME)}</SVCURRENTCOMPANY>
     <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
     <GROUPNAME>{escape(group)}</GROUPNAME>
     <SVFROMDATE>{_fmt_date(_fy_start(upto))}</SVFROMDATE>
     <SVTODATE>{_fmt_date(upto)}</SVTODATE>
    </STATICVARIABLES>
   </REQUESTDESC>
  </EXPORTDATA>
 </BODY>
</ENVELOPE>"""
    root = _post_xml(xml_req, dump_raw_dir, "group_summary_" + re.sub(r"\W+", "_", group).strip("_").lower(),
                     timeout=timeout, max_attempts=1)
    lines, name = [], None
    for el in root.iter():
        if el.tag == "DSPACCNAME":
            name = _text(el, "DSPDISPNAME")
        elif el.tag == "DSPACCINFO" and name is not None:
            dr = _num(el.find("DSPCLDRAMT") if el.find("DSPCLDRAMT") is not None else el, "DSPCLDRAMTA")
            cr = _num(el.find("DSPCLCRAMT") if el.find("DSPCLCRAMT") is not None else el, "DSPCLCRAMTA")
            lines.append({"name": name, "closing": round(-(dr + cr), 2)})
            name = None
    return lines


def _affects_books(v, type_parents):
    base = type_parents.get(_text(v, "VOUCHERTYPENAME").strip().lower())
    return (base in ACCOUNTING_BASES and _text(v, "ISOPTIONAL").lower() != "yes"
            and _text(v, "ISCANCELLED").lower() != "yes")


def _bill_date(raw):
    """A date as Tally's bill reports print it -- "1-Apr-26", "01-Apr-2026"
    or YYYYMMDD -- as YYYY-MM-DD, or "" when it can't be read."""
    raw = (raw or "").strip()
    for fmt in ("%d-%b-%y", "%d-%b-%Y", "%Y%m%d", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def fetch_bills(kind, upto, dump_raw_dir=None, timeout=60):
    """Every pending bill in Tally's own Bills Receivable (kind "receivable",
    the Sundry Debtors' unpaid invoices) or Bills Payable ("payable") report
    as at upto: [{party, ref, date, due, overdue_days, amount}], amount as
    owed -- positive for a debtor's unpaid invoice or an unpaid supplier
    bill, negative for an advance. Only parties kept bill-by-bill in Tally
    appear. A report, one attempt, a time limit: the same care as the
    other balance reads, see fetch_group_summary.

    The export is a flat run per bill of <BILLFIXED> (BILLDATE, BILLREF,
    BILLPARTY) followed by its siblings BILLCL (the pending amount, a debit
    negative), BILLDUE and BILLOVERDUE (days past due)."""
    from xml.sax.saxutils import escape
    report = "Bills Receivable" if kind == "receivable" else "Bills Payable"
    xml_req = f"""<ENVELOPE>
 <HEADER>
  <TALLYREQUEST>Export Data</TALLYREQUEST>
 </HEADER>
 <BODY>
  <EXPORTDATA>
   <REQUESTDESC>
    <REPORTNAME>{report}</REPORTNAME>
    <STATICVARIABLES>
     <SVCURRENTCOMPANY>{escape(TALLY_COMPANY_NAME)}</SVCURRENTCOMPANY>
     <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
     <SVFROMDATE>{_fmt_date(_fy_start(upto))}</SVFROMDATE>
     <SVTODATE>{_fmt_date(upto)}</SVTODATE>
    </STATICVARIABLES>
   </REQUESTDESC>
  </EXPORTDATA>
 </BODY>
</ENVELOPE>"""
    root = _post_xml(xml_req, dump_raw_dir, "bills_" + kind, timeout=timeout, max_attempts=1)
    sign = -1 if kind == "receivable" else 1
    bills, cur = [], None
    for el in root.iter():
        if el.tag == "BILLFIXED":
            cur = {"party": _text(el, "BILLPARTY"), "ref": _text(el, "BILLREF"),
                   "date": _bill_date(_text(el, "BILLDATE")), "due": "", "overdue_days": None, "amount": 0.0}
            bills.append(cur)
        elif cur is not None and el.tag == "BILLCL":
            try:
                cur["amount"] = round(sign * float((el.text or "0").replace(",", "")), 2)
            except ValueError:
                pass
        elif cur is not None and el.tag == "BILLDUE":
            cur["due"] = _bill_date(el.text)
        elif cur is not None and el.tag == "BILLOVERDUE":
            try:
                cur["overdue_days"] = int(float((el.text or "").replace(",", "")))
            except ValueError:
                pass
    return bills


def oldest_bills_by_party(bills, today):
    """{party: {"oldest": YYYY-MM-DD, "days": days since that bill's date,
    "bills": how many are pending, "over_60": what those of them raised
    OVERDUE_REPORT_DAYS or more days ago add up to}} -- the oldest unpaid
    bill of each party, counting only bills still owed (an advance isn't a
    bill waiting to be paid)."""
    out = {}
    cutoff = (today - datetime.timedelta(days=OVERDUE_REPORT_DAYS)).isoformat()
    for b in bills:
        if b["amount"] <= 0.5 or not b["date"]:
            continue
        p = out.setdefault(b["party"], {"oldest": b["date"], "bills": 0, "over_60": 0.0})
        p["bills"] += 1
        p["oldest"] = min(p["oldest"], b["date"])
        if b["date"] <= cutoff:
            p["over_60"] = round(p["over_60"] + b["amount"], 2)
    for p in out.values():
        p["days"] = (today - datetime.date.fromisoformat(p["oldest"])).days
    return out


class _GroupBalances:
    """Group Summary closing balances for one sync, each group and day asked
    of Tally once and reused -- a day's closing is the next day's opening,
    and cash and bank share nothing else. After Tally fails to answer one in
    time no more are asked for, in this sync or for BALANCES_PAUSE_HOURS
    after: a Tally that's struggling isn't asked again every few minutes."""

    def __init__(self, dump_raw_dir=None):
        self.dump_raw_dir = dump_raw_dir
        self.cache = {}
        until = _read_marker(BALANCES_PAUSE_FILE)
        self.stopped = (f"paused until {until:%d %b %H:%M} after Tally didn't answer in time"
                        if until and until > datetime.datetime.now() else None)

    def closing(self, group, date):
        """{line name: closing balance, debit positive} for group at the end of date."""
        if self.stopped:
            raise TallyError(f"Balances not asked for -- {self.stopped}.")
        key = (group.lower(), date)
        if key not in self.cache:
            try:
                lines = fetch_group_summary(group, date, self.dump_raw_dir)
            except TallyTimeout as e:
                until = datetime.datetime.now() + datetime.timedelta(hours=BALANCES_PAUSE_HOURS)
                _write_marker(BALANCES_PAUSE_FILE, until)
                self.stopped = f"paused until {until:%d %b %H:%M} after Tally didn't answer in time"
                log.warning("Balances paused until %s: %s", until, e)
                raise
            self.cache[key] = {line["name"]: line["closing"] for line in lines}
        return self.cache[key]


def _ledger_movement(vouchers, ledger_names, type_parents):
    """The day's net change across ledger_names from its own vouchers that
    post to the books, debits (money in) positive. A transfer between two
    of them -- cash to petty cash, one bank to another -- nets to nothing."""
    total = 0.0
    for v in vouchers:
        if not _affects_books(v, type_parents):
            continue
        for entry in v.findall(".//ALLLEDGERENTRIES.LIST"):
            if (entry.get("NAME") or _text(entry, "LEDGERNAME") or "").strip() in ledger_names:
                total -= _num(entry, "AMOUNT")
    return total


def _balance_for(date, vouchers, ledger_names, type_parents, balances, what):
    """Opening and closing across ledger_names for one day -- cash in hand,
    or money in the bank -- in total and per ledger, from the Group Summary
    of each group they're under. Opening is the closing at the end of the
    day before, as on Tally's Cash and Bank Books.

    "matches" is the check that opening + the day's entries = closing to
    the rupee; the daily emails print the balances only when it holds. A
    failure here never stops the rest of the day's sync."""
    groups = sorted({_LEDGER_PARENTS[n] for n in ledger_names if _LEDGER_PARENTS.get(n)})
    if not groups:
        return None
    before, after = {}, {}
    try:
        for group in groups:
            before.update(balances.closing(group, date - datetime.timedelta(days=1)))
            after.update(balances.closing(group, date))
    except TallyError as e:
        log.warning("%s: %s balances not read -- %s", date, what, e)
        return {"error": str(e)}
    ledgers = {n: {"opening": round(before.get(n, 0.0), 2), "closing": round(after.get(n, 0.0), 2)}
               for n in sorted(ledger_names)}
    opening = round(sum(b["opening"] for b in ledgers.values()), 2)
    closing = round(sum(b["closing"] for b in ledgers.values()), 2)
    movement = round(_ledger_movement(vouchers, ledger_names, type_parents), 2)
    matches = abs(opening + movement - closing) < 1
    if not matches:
        log.warning("%s: %s balances don't tie -- opening %.2f + movement %.2f != closing %.2f",
                    date, what, opening, movement, closing)
    return {"opening": opening, "closing": closing, "movement": movement, "matches": matches,
            "ledgers": ledgers, "source": "Tally Group Summary"}


def fetch_party_balances(date, balances):
    """Every line under Sundry Debtors and Sundry Creditors at the end of
    date, from Tally's Group Summary of each, for the dashboard's two tiles.
    A party filed under a sub-group shows as that sub-group's one line, as
    on Tally's own screen. Debtors read as receivable and creditors as
    payable, so both totals are positive the way they're spoken of; a
    customer's advance or a supplier's debit note comes out negative and
    reduces the total, as in Tally. Nil balances are left out."""
    result = {}
    for key, group, sign in (("debtors", "Sundry Debtors", 1), ("creditors", "Sundry Creditors", -1)):
        lines = balances.closing(group, date)
        # A company Tally can't read answers with nothing, not an error --
        # on 5 Oct an empty answer wrote Rs.0 over the real figures.
        if key == "debtors" and not lines:
            raise TallyError("Sundry Debtors came back empty. " + UNREADABLE_COMPANY_HINT)
        parties = [{"name": name, "group": _LEDGER_PARENTS.get(name) or "Sub-group", "amount": round(sign * amount, 2)}
                   for name, amount in lines.items() if abs(amount) >= 0.5]
        parties.sort(key=lambda r: -r["amount"])
        total = round(sum(r["amount"] for r in parties), 2)
        result[key] = {"total": total, "count": len(parties), "parties": parties,
                       "tally_total": total, "matches": True, "source": "Tally Group Summary"}
    return result


def _bill_descriptions(vouchers):
    """{(party, voucher number): what it was for} from the year's vouchers,
    to say what each pending bill is: a bill's reference in Tally's Bills
    Receivable is the number of the invoice that raised it. Matched with the
    party as well, since two voucher series can reuse a number. A bill from
    before this financial year has no voucher here and goes without."""
    out = {}
    for v in vouchers or []:
        no = _text(v, "VOUCHERNUMBER").strip()
        party = _text(v, "PARTYLEDGERNAME").strip()
        if no and party:
            out.setdefault((party.lower(), no), _voucher_description(v))
    return out


def _add_days_pending(result, today, balances, dump_raw_dir=None, descriptions=None):
    """Adds to each party on the Debtors/Creditors tiles its oldest unpaid
    bill -- "oldest" (YYYY-MM-DD), "days" since it and "bills" pending --
    from Tally's own Bills Receivable / Bills Payable (fetch_bills). On
    5 Oct those answered in 1.0 s and 0.2 s.

    Only parties already on the tile get it, matched by name: the bills
    reports also cover loan and staff ledgers outside Sundry Debtors and
    Creditors ("Samarth B Patel Loan", "Pravina Narendra Doshi Loan"), which
    is why their totals ran Rs.25 lakh and Rs.1.1 crore above the groups'.
    A sub-group's line gets the oldest bill of the parties under it. A
    party Tally doesn't keep bill by bill simply has no days. Never stops
    the sync: a failure leaves the balances without days, and a timeout
    pauses the balance reads like any other (_GroupBalances).

    Each debtor also gets "bills_list": every bill it still owes, oldest
    first, with its number, date, amount and -- from descriptions
    (_bill_descriptions) -- what the invoice was for. The weekly Debtors
    Pending 60+ Days email and its PDF list them under each party."""
    if balances.stopped:
        return
    for key, kind in (("debtors", "receivable"), ("creditors", "payable")):
        try:
            bills = fetch_bills(kind, today, dump_raw_dir, timeout=GROUP_SUMMARY_TIMEOUT_SECONDS)
        except TallyTimeout as e:
            until = datetime.datetime.now() + datetime.timedelta(hours=BALANCES_PAUSE_HOURS)
            _write_marker(BALANCES_PAUSE_FILE, until)
            balances.stopped = f"paused until {until:%d %b %H:%M} after Tally didn't answer in time"
            log.warning("Bills %s not read, balances paused until %s: %s", kind, until, e)
            return
        except TallyError as e:
            log.warning("Bills %s not read -- %s", kind, e)
            continue
        by_party = oldest_bills_by_party(bills, today)
        owed = {}
        if key == "debtors":
            for b in sorted(bills, key=lambda b: b["date"] or "9999"):
                if b["amount"] > 0.5 and b["date"]:
                    owed.setdefault(b["party"], []).append({
                        "party": b["party"], "ref": b["ref"], "date": b["date"], "amount": b["amount"],
                        "description": (descriptions or {}).get((b["party"].lower(), b["ref"].strip()), "")})
        under = {}
        for party, info in by_party.items():
            parent = _LEDGER_PARENTS.get(party)
            if parent:
                u = under.setdefault(parent, {"oldest": info["oldest"], "bills": 0, "over_60": 0.0})
                u["oldest"] = min(u["oldest"], info["oldest"])
                u["bills"] += info["bills"]
                u["over_60"] = round(u["over_60"] + info["over_60"], 2)
        dated = 0
        for row in result[key]["parties"]:
            if owed:
                # A sub-group's line lists the bills of every party under it.
                row["bills_list"] = owed.get(row["name"]) or sorted(
                    (b for party, bs in owed.items() if _LEDGER_PARENTS.get(party) == row["name"] for b in bs),
                    key=lambda b: b["date"])
            info = by_party.get(row["name"]) or under.get(row["name"])
            if info:
                row.update(oldest=info["oldest"], bills=info["bills"], over_60=info["over_60"],
                           days=(today - datetime.date.fromisoformat(info["oldest"])).days)
                dated += 1
        result[key]["bills_read"] = True
        log.info("Bills %s: %d bills; %d of %d parties dated.", kind, len(bills), dated, len(result[key]["parties"]))


def _sync_party_balances(balances, dry_run=False, force=False, vouchers=None):
    """Reads and stores today's Sundry Debtors/Creditors: on every daily
    run (force), otherwise at most every PARTY_BALANCES_EVERY_MINUTES.
    A failure is logged and leaves the last stored figures in place -- it
    never stops the sync."""
    last = _read_marker(PARTY_BALANCES_MARKER)
    if not force and last and datetime.datetime.now() - last < datetime.timedelta(minutes=PARTY_BALANCES_EVERY_MINUTES):
        log.info("Sundry debtors/creditors last read at %s -- not asked again yet.", f"{last:%H:%M}")
        return
    try:
        result = fetch_party_balances(datetime.date.today(), balances)
    except TallyError as e:
        log.warning("Sundry debtors/creditors not read -- %s", e)
        return
    _add_days_pending(result, datetime.date.today(), balances, balances.dump_raw_dir, _bill_descriptions(vouchers))
    log.info("Sundry debtors Rs.%s (%d parties) | Sundry creditors Rs.%s (%d parties)",
             result["debtors"]["total"], result["debtors"]["count"],
             result["creditors"]["total"], result["creditors"]["count"])
    if dry_run:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    try:
        push_party_balances(result)
        _write_marker(PARTY_BALANCES_MARKER, datetime.datetime.now())
    except Exception as e:
        log.warning("Sundry debtors/creditors not written -- %s", e)


def fetch_stock_summary(upto, dump_raw_dir=None, timeout=60, items=False, report="Stock Summary"):
    """Tally's own Stock Summary report as at upto, top level: each stock
    group (or item not in a group) with its closing quantity and value --
    the screen Gateway > Stock Summary shows. A report rather than a
    StockItem collection for the same reason as fetch_group_summary: on
    5 Oct 2026 per-object balance requests hung Tally, while its own
    reports answered in a second or less. One attempt only.

    The export is a flat run of <DSPACCNAME><DSPDISPNAME>name</DSPDISPNAME>
    </DSPACCNAME> each followed by a <DSPSTKINFO> holding the closing
    figures (DSPCLQTY, DSPCLRATE, DSPCLAMTA). Tally writes the value of
    stock, a debit, as a negative number; it's returned as a positive one.

    items=True asks for the report exploded, item by item -- what F5 does on
    Tally's own Stock Summary screen. On 5 Oct the plain report came back as
    one line in 0.2 s, matching the P&L closing stock to the paisa.

    report="Stock Category Summary" is the same export by stock category --
    the screen the owner reads stock from -- whose exploded form lists every
    item under its category, with the rate as well."""
    from xml.sax.saxutils import escape
    xml_req = f"""<ENVELOPE>
 <HEADER>
  <TALLYREQUEST>Export Data</TALLYREQUEST>
 </HEADER>
 <BODY>
  <EXPORTDATA>
   <REQUESTDESC>
    <REPORTNAME>{escape(report)}</REPORTNAME>
    <STATICVARIABLES>
     <SVCURRENTCOMPANY>{escape(TALLY_COMPANY_NAME)}</SVCURRENTCOMPANY>
     <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
     <SVFROMDATE>{_fmt_date(_fy_start(upto))}</SVFROMDATE>
     <SVTODATE>{_fmt_date(upto)}</SVTODATE>
     <EXPLODEFLAG>{"Yes" if items else "No"}</EXPLODEFLAG>
    </STATICVARIABLES>
   </REQUESTDESC>
  </EXPORTDATA>
 </BODY>
</ENVELOPE>"""
    dump_name = re.sub(r"\W+", "_", report).strip("_").lower() + ("_items" if items else "")
    root = _post_xml(xml_req, dump_raw_dir, dump_name, timeout=timeout, max_attempts=1)
    lines, name = [], None
    for el in root.iter():
        if el.tag == "DSPACCNAME":
            name = _text(el, "DSPDISPNAME")
        elif el.tag == "DSPSTKINFO" and name is not None:
            qty = next((x.text.strip() for x in el.iter("DSPCLQTY") if x.text and x.text.strip()), "")
            amount = next((x.text.strip() for x in el.iter("DSPCLAMTA") if x.text and x.text.strip()), "")
            rate = next((x.text.strip() for x in el.iter("DSPCLRATE") if x.text and x.text.strip()), "")
            try:
                value = -float(amount.replace(",", "")) if amount else 0.0
            except ValueError:
                value = 0.0
            lines.append({"name": name, "qty_text": qty, "rate_text": rate, "value": round(value, 2)})
            name = None
    return lines


def fetch_stock_groups(date, dump_raw_dir=None):
    """(total, groups) for the Stock tile: Tally's Stock Summary total as at
    date, and the lines one level down, each with its quantity and value.

    Asked twice, plain and exploded, because the exploded export is the same
    flat run of lines with the parent lines still in it: on 5 Oct it came
    back as "No" (Rs.3,53,782.02) followed by the four groups under it,
    which add up to the same figure -- summing every line doubled the stock.
    The plain report's lines are the top level, so the groups are whatever
    the exploded one has that the plain one doesn't, and the total is the
    plain report's own. If those groups don't add up to it, the top-level
    lines are listed instead: never a list that counts anything twice."""
    top = fetch_stock_summary(date, dump_raw_dir, timeout=STOCK_TIMEOUT_SECONDS)
    # A company Tally can't read answers with nothing; 5 Oct wrote "0 items"
    # that way.
    if not top:
        raise TallyError("Stock Summary came back empty. " + UNREADABLE_COMPANY_HINT)
    total = round(sum(r["value"] for r in top), 2)
    exploded = fetch_stock_summary(date, dump_raw_dir, timeout=STOCK_TIMEOUT_SECONDS, items=True)
    top_names = {r["name"] for r in top}
    under = [r for r in exploded if r["name"] not in top_names]
    if under and abs(sum(r["value"] for r in under) - total) < 1:
        parent = top[0]["name"] if len(top) == 1 else ""
        groups = [dict(r, group=parent) for r in under]
    else:
        if under:
            log.warning("Stock groups add up to Rs.%.2f, not the Stock Summary total Rs.%.2f -- listing the top level.",
                        sum(r["value"] for r in under), total)
        groups = [dict(r, group="") for r in top]
    return total, groups


class StockShapeError(Exception):
    """Tally answered, but not in a shape the item list can be built from
    without guessing -- the stock tile falls back to the groups."""


def fetch_stock_category_names(date, dump_raw_dir=None):
    """{category name: parent name} for every Stock Category in Tally, from
    the masters alone -- a list like the ledger list, which answered in
    0.2 s, with nothing for Tally to total. "Not Applicable" is added: it's
    where Tally's Stock Category Summary puts items with no category."""
    root = _post_xml(_collection_request("StockCatList", "StockCategory", ["NAME", "PARENT"], date, date),
                     dump_raw_dir, "stock_category_list", timeout=STOCK_TIMEOUT_SECONDS, max_attempts=1)
    names = {}
    for c in _collection_records(root, "STOCKCATEGORY"):
        name = (c.get("NAME") or _text(c, "NAME") or "").strip()
        if name:
            names[name] = _text(c, "PARENT").strip()
    names.setdefault("Not Applicable", "")
    return names


def _split_by_category(lines, categories, top_items=frozenset()):
    """Splits a Stock Category Summary's flat run of lines into categories
    and their items. A line named after a category starts it; a line in
    top_items -- an item the plain report lists at the top level, i.e. one
    with no category -- is an item of no category; every other line is an
    item of the category before it. Returns (items, checks): checks has one
    row per category -- its own line's value, and how many items followed
    it and what they add up to -- and an item's "group" is its category,
    "" for none. A category with sub-categories has no items of its own;
    its line is checked against nothing and its value isn't counted twice."""
    items, checks, current = [], [], None
    for r in lines:
        if r["name"] in categories:
            current = {"name": r["name"], "value": r["value"], "items": 0, "items_value": 0.0,
                       "parent_only": r["name"] in set(categories.values())}
            checks.append(current)
        elif r["name"] in top_items:
            items.append(dict(r, group=""))
        elif current is None:
            checks.append({"name": "(before any category)", "value": 0.0, "items": 1,
                           "items_value": r["value"], "parent_only": False})
        else:
            items.append(dict(r, group=current["name"]))
            current["items"] += 1
            current["items_value"] += r["value"]
    for c in checks:
        c["ok"] = (c["items"] == 0 and c["parent_only"]) or abs(c["items_value"] - c["value"]) < 1
    return items, checks


def fetch_stock_by_category(date, dump_raw_dir=None, report_checks=None):
    """(total, items) from Tally's Stock Category Summary as at date: every
    item with its quantity, rate, value and the category it's under -- the
    screen the owner reads stock from (Display > Stock Category Summary).

    The report is read twice. The plain one is the top level: each category
    as one line, and every item with no category as a line of its own --
    on 5 Oct that was "No." (Rs.58,638.68), the only category, and 216
    items with none (Rs.3,43,166.43). The exploded one is the same with
    each category's items after it: "No." and its 15 items, and the same
    216. Which lines are categories comes from the category masters
    (fetch_stock_category_names). So in the exploded report a category line
    starts that category, an item the plain report already listed has no
    category, and any other item belongs to the category before it.

    Used only when every category's items add up to its own line and all
    the items add up to the plain report's total -- otherwise
    StockShapeError, never a list that counts something twice or leaves
    something out. report_checks, a list, gets the per-category check for
    --test-stock to print."""
    report = "Stock Category Summary"
    categories = fetch_stock_category_names(date, dump_raw_dir)
    plain = fetch_stock_summary(date, dump_raw_dir, timeout=STOCK_TIMEOUT_SECONDS, report=report)
    if not plain:
        raise StockShapeError("Stock Category Summary came back empty")
    total = round(sum(r["value"] for r in plain), 2)
    top_items = {r["name"] for r in plain if r["name"] not in categories}
    exploded = fetch_stock_summary(date, dump_raw_dir, timeout=STOCK_TIMEOUT_SECONDS, items=True, report=report)
    items, checks = _split_by_category(exploded, categories, top_items)
    if report_checks is not None:
        report_checks.append((f"exploded, {len(top_items)} items with no category", len(exploded), checks))
    bad = [c["name"] for c in checks if not c["ok"]]
    if bad:
        raise StockShapeError(f"{len(bad)} categories don't add up ({', '.join(bad[:3])})")
    items_total = round(sum(r["value"] for r in items), 2)
    if not items or abs(items_total - total) >= 1:
        raise StockShapeError(f"{len(items)} items add up to Rs.{items_total:.2f}, not the report's Rs.{total:.2f}")
    return total, items


# Voucher types that bring stock in, for the age of what's in stock.
STOCK_INWARD_BASES = {"purchase", "receipt note", "credit note", "stock journal", "material in", "rejections in"}


def _leading_qty(text):
    m = re.match(r"\s*(-?[\d,]*\.?\d+)", text or "")
    return float(m.group(1).replace(",", "")) if m else 0.0


def add_stock_ages(items, vouchers, type_parents, today):
    """Adds to each item held in stock "since" (YYYY-MM-DD) and "age_days":
    how long the oldest of what's in hand has been there, first in first
    out. Walking the item's inward entries -- purchases, receipt notes,
    sales returns, the inward side of a stock journal -- from the newest
    back, the date at which they add up to the quantity held is when the
    oldest unit still in stock came in. When this year's inward entries
    don't reach the quantity held, the rest is opening stock: "since" is
    the first day of the financial year and "from_opening" is set, so it
    reads "before 1 April". For the weekly stock email, oldest first."""
    fy = _fy_start(today)
    inward = {}
    for v in vouchers or []:
        base = type_parents.get(_text(v, "VOUCHERTYPENAME").strip().lower())
        if base not in STOCK_INWARD_BASES or _text(v, "ISOPTIONAL").lower() == "yes" \
                or _text(v, "ISCANCELLED").lower() == "yes":
            continue
        day = _tally_date_to_iso(_text(v, "DATE"))
        for inv in v.findall(".//ALLINVENTORYENTRIES.LIST"):
            if _text(inv, "ISDEEMEDPOSITIVE").lower() == "no":
                continue          # the outward side of a stock journal
            name = (inv.get("NAME") or _text(inv, "STOCKITEMNAME") or "").strip()
            qty = abs(_leading_qty(_text(inv, "ACTUALQTY") or _text(inv, "BILLEDQTY")))
            if name and qty and day <= today.isoformat():
                inward.setdefault(name, []).append((day, qty))
    for item in items:
        held = _leading_qty(item.get("qty_text"))
        if held <= 0:
            continue
        reached, since = 0.0, None
        for day, qty in sorted(inward.get(item.get("name"), []), reverse=True):
            reached += qty
            since = day
            if reached >= held - 1e-9:
                break
        from_opening = reached < held - 1e-9
        if from_opening:
            since = fy.isoformat()
        item.update(since=since, from_opening=from_opening,
                    age_days=(today - datetime.date.fromisoformat(since)).days)
    return items


def _stock_marker(name):
    return os.path.join(SCRIPT_DIR, name)


def _read_marker(name):
    try:
        with open(_stock_marker(name)) as f:
            return datetime.datetime.fromisoformat(f.read().strip())
    except (OSError, ValueError):
        return None


def _write_marker(name, when):
    with open(_stock_marker(name), "w") as f:
        f.write(when.isoformat())


def _sync_stock(date, pl_closing_stock=None, dry_run=False, dump_raw_dir=None, vouchers=None, type_parents=None):
    """Reads the stock by group from Tally's Stock Summary and stores it, as
    carefully as Tally needs:
      - at most once every STOCK_MIN_HOURS, however often syncs run -- the
        hourly and Sync now runs mostly skip it;
      - one attempt per request with a STOCK_TIMEOUT_SECONDS limit;
      - after Tally fails to answer in time, not asked again for
        STOCK_PAUSE_HOURS, and the dashboard says so. Tally being closed
        isn't a reason to pause: the next sync simply tries again.
    pl_closing_stock is the P&L's Closing Stock for the same day, asked for
    here when not given; the stock total is checked against it. Never
    stops the sync."""
    now = datetime.datetime.now()
    paused_until = _read_marker("stock_paused_until.txt")
    if not dry_run and paused_until and paused_until > now:
        log.info("Stock paused until %s after Tally didn't answer in time.", f"{paused_until:%d %b %H:%M}")
        return
    last = _read_marker("stock_last_read.txt")
    if not dry_run and last and now - last < datetime.timedelta(hours=STOCK_MIN_HOURS):
        return
    started = time.time()
    try:
        try:
            total, groups = fetch_stock_by_category(date, dump_raw_dir)
            level = "item"
        except TallyTimeout:
            raise
        except (StockShapeError, TallyError) as e:
            log.warning("Stock by category not usable (%s) -- reading it by stock group instead.", e)
            total, groups = fetch_stock_groups(date, dump_raw_dir)
            level = "group"
    except TallyTimeout as e:
        until = now + datetime.timedelta(hours=STOCK_PAUSE_HOURS)
        log.warning("Stock not read (%s) -- not asking again until %s.", e, f"{until:%d %b %H:%M}")
        if not dry_run:
            _write_marker("stock_paused_until.txt", until)
            try:
                _firestore_db().collection(PERIOD_COLLECTION).document(STOCK_DOC).set(
                    {"error": str(e)[:300], "paused_until": until.date().isoformat()}, merge=True)
            except Exception as ex:
                log.warning("Stock status not written -- %s", ex)
        return
    except TallyError as e:
        log.warning("Stock not read -- %s", e)
        return
    took = time.time() - started
    if level == "item" and vouchers:
        add_stock_ages(groups, vouchers, type_parents or {}, date)
    if pl_closing_stock is None:
        try:
            pl_closing_stock = fetch_profit_and_loss(date, dump_raw_dir).get("closing_stock")
        except TallyError as e:
            log.warning("P&L closing stock not read for the stock check -- %s", e)
    matches = None if pl_closing_stock is None else abs(total - pl_closing_stock) < 1
    log.info("Stock: Rs.%s in %d %ss (%.1f s)%s", total, len(groups), level, took,
             "" if matches is not False else f" -- P&L closing stock is Rs.{pl_closing_stock}")
    doc = {"as_of": datetime.datetime.now(datetime.timezone.utc).isoformat(), "date": date.isoformat(),
           "level": level, "total_value": total, "count": len(groups), "items": groups,
           "seconds": round(took, 1), "pl_closing_stock": pl_closing_stock, "matches": matches,
           "error": None, "paused_until": None}
    if dry_run:
        print(json.dumps(doc, indent=2, ensure_ascii=False))
        return
    try:
        _firestore_db().collection(PERIOD_COLLECTION).document(STOCK_DOC).set(doc)
        _write_marker("stock_last_read.txt", now)
    except Exception as e:
        log.warning("Stock not written -- %s", e)


def push_pending_proformas(records, covered_from):
    """Replaces the pending Proforma list with what Tally has now, keeping
    any entry dated before covered_from -- the same limit as the Delivery
    Challans below: Tally only hands back the current financial year, so on
    1 April a proforma from March is out of view, not deleted, and has to
    stay listed. Unlike the challans an empty list is written as it is:
    proformas are picked by name, not by a classification that could fail,
    and a fetch with no vouchers at all has already been refused, so none
    found means none pending. Returns how many are listed."""
    doc_ref = _firestore_db().collection(PERIOD_COLLECTION).document(PENDING_PROFORMA_DOC)
    before = doc_ref.get()
    earlier = [r for r in ((before.to_dict() or {}).get("proformas") or [] if before.exists else [])
               if (r.get("date") or "") < covered_from]
    listed = earlier + records
    doc_ref.set({
        "synced_at": datetime.datetime.now().isoformat(),
        "covered_from": covered_from,
        "proformas": listed,
    })
    return len(listed)


def push_delivery_challans_to_firestore(records, covered_from):
    """Makes DELIVERY_CHALLAN_COLLECTION a copy of the Delivery Notes Tally
    has now: writes each one whole, one document per voucher keyed by
    _delivery_challan_doc_id, then removes documents for Delivery Notes
    Tally no longer has. Returns how many were removed.

    Each write replaces the whole document rather than merging, so nothing
    but what Tally said on this run survives -- including any billed/
    billed_at/billed_by fields left from when the dashboard briefly had a
    Mark Billed button.

    The removal exists because writing alone only ever adds. The first
    live run showed 49 pending challans on the dashboard against 48 Delivery
    Notes in Tally: a Delivery Note edited to a new date or number gets a new
    document and the old one stayed pending for good, and one deleted in
    Tally (which also happens here once it's invoiced -- 55 Delivery Notes on
    23 Sep, 48 on 3 Oct) did the same. Two limits keep this from deleting
    anything it shouldn't:
      - only documents dated on or after covered_from, the earliest voucher
        date in this fetch, are considered. Tally only hands back the current
        financial year, so on 1 April a still-pending challan from March is
        merely out of view, not deleted, and has to stay;
      - nothing is removed when this run found no Delivery Notes at all,
        which looks the same as voucher types failing to classify, and
        emptying the whole list on a bad read is the worse mistake.
    """
    db = _firestore_db()
    collection = db.collection(DELIVERY_CHALLAN_COLLECTION)
    batch = db.batch()
    pending = 0

    def queued():
        # Firestore caps a single batch at 500 writes -- commit in chunks well
        # under that rather than assume the challan count never grows past it.
        nonlocal batch, pending
        pending += 1
        if pending >= 400:
            batch.commit()
            batch = db.batch()
            pending = 0

    for rec in records:
        data = {k: v for k, v in rec.items() if k != "doc_id"}
        batch.set(collection.document(rec["doc_id"]), data)
        queued()

    removed = 0
    if records:
        current_ids = {rec["doc_id"] for rec in records}
        for doc in collection.stream():
            data = doc.to_dict() or {}
            if doc.id in current_ids:
                continue
            if (data.get("date") or "") < covered_from:
                continue
            batch.delete(doc.reference)
            queued()
            removed += 1

    if pending:
        batch.commit()
    return removed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _fy_start(d):
    """1 April of the financial year d falls in (1 April - 31 March)."""
    return datetime.date(d.year if d.month >= 4 else d.year - 1, 4, 1)


def _add_months(first_of_month, n):
    m = first_of_month.month - 1 + n
    return datetime.date(first_of_month.year + m // 12, m % 12 + 1, 1)


def period_range(period, d):
    """(start, end) of the period containing d, matching plPeriodRange in
    index.html exactly: weeks run Monday-Sunday, months are calendar months,
    and quarters, half-years and years count from 1 April."""
    if period == "weekly":
        start = d - datetime.timedelta(days=d.weekday())
        return start, start + datetime.timedelta(days=6)
    if period == "monthly":
        start = d.replace(day=1)
        return start, _add_months(start, 1) - datetime.timedelta(days=1)
    size = {"quarterly": 3, "half_yearly": 6, "yearly": 12}[period]
    fy = _fy_start(d)
    months_in = (d.year - fy.year) * 12 + d.month - fy.month
    start = _add_months(fy, months_in // size * size)
    return start, _add_months(start, size) - datetime.timedelta(days=1)


def periods_touching(from_date, to_date):
    """Every (period, start, end) containing at least one day of
    from_date..to_date -- the periods a sync of those days can change."""
    found = {}
    d = from_date
    while d <= to_date:
        for period in PERIOD_NAMES:
            start, end = period_range(period, d)
            found[(period, start)] = end
        d += datetime.timedelta(days=1)
    return [(period, start, end) for (period, start), end in sorted(found.items(), key=lambda kv: (kv[0][1], kv[0][0]))]


def sync_period_reports(from_date, to_date, dry_run=False, dump_raw_dir=None):
    """Asks Tally for its own P&L for every period the synced days fall in,
    and stores each as one document. A period still in progress is
    requested up to today and marked complete=False.

    This replaced adding up daily figures on the dashboard, which could not
    give the real profit: a period's profit includes the change in stock
    over the whole period (closing stock at its end minus opening stock at
    its start), and summed daily stock is meaningless -- so the sum left
    stock out entirely and showed income minus expenses instead. For
    1 Apr - 15 Sep 2026 that read Rs.2,02,36,465 against Tally's real
    profit of Rs.1,41,47,775; the Rs.60.9 lakh difference was the fall in
    stock value. Asking Tally for the period gives its own figure, stock
    included, with nothing worked out here.

    One period failing is logged and skipped, like a day in a backfill --
    the next sync covering it tries again.
    """
    today = datetime.date.today()
    periods = periods_touching(from_date, to_date)
    written, failed = 0, []
    for period, start, end in periods:
        upto = min(end, today)
        doc_id = f"{period}_{start.isoformat()}"
        try:
            pl = fetch_profit_and_loss(start, dump_raw_dir, to_date=upto)
        except TallyError as e:
            log.error("%s (%s to %s): skipped -- %s", doc_id, start, upto, e)
            failed.append(doc_id)
            continue
        log.info("%s: %s to %s%s | P&L %s", period, start, upto, "" if upto == end else " (so far)",
                 "needs review" if pl["needs_review"] else f"Rs.{pl['net_profit_loss']}")
        if not dry_run:
            push_period_report(doc_id, {
                "period": period,
                "from": start.isoformat(),
                "to": upto.isoformat(),
                "complete": upto == end,
                "synced_at": datetime.datetime.now().isoformat(),
                "profit_and_loss": pl,
            })
        written += 1
    log.info("Period P&L: %d/%d periods %s%s.", written, len(periods),
             "fetched (dry run, not written)" if dry_run else "written",
             f", {len(failed)} failed ({', '.join(failed)})" if failed else "")


def _build_payload(date, vouchers, voucher_type_parents, cash_ledgers, bank_ledgers, dump_raw_dir=None,
                   balances=None):
    recent = balances is not None and (datetime.date.today() - date).days <= BALANCE_DAYS
    cash_balance = _balance_for(date, vouchers, cash_ledgers, voucher_type_parents, balances, "cash") if recent else None
    bank_balance = _balance_for(date, vouchers, bank_ledgers, voucher_type_parents, balances, "bank") if recent else None
    payload = {
        "date": date.strftime("%Y-%m-%d"),
        "synced_at": datetime.datetime.now().isoformat(),
        "sales": _filter_by_class(vouchers, voucher_type_parents, "sales"),
        "proforma": _filter_by_class(vouchers, voucher_type_parents, "sales", proforma=True),
        "purchase": _filter_by_class(vouchers, voucher_type_parents, "purchase"),
        "profit_and_loss": fetch_profit_and_loss(date, dump_raw_dir),
        "cash_vouchers": _cash_vouchers_from(vouchers, cash_ledgers),
        **({"cash_balance": cash_balance} if cash_balance is not None else {}),
        **({"bank_balance": bank_balance} if bank_balance is not None else {}),
        "bank_vouchers": _bank_vouchers_from(vouchers, bank_ledgers),
    }
    log.info(
        "%s: Sales Rs.%s (%d vch) | Proforma Rs.%s (%d vch) | Purchase Rs.%s (%d vch) | P&L %s | Cash vouchers %d | Bank vouchers %d",
        payload["date"],
        payload["sales"]["total"], payload["sales"]["count"],
        payload["proforma"]["total"], payload["proforma"]["count"],
        payload["purchase"]["total"], payload["purchase"]["count"],
        ("needs review" if payload["profit_and_loss"]["needs_review"]
         else f"Rs.{payload['profit_and_loss']['net_profit_loss']}"),
        payload["cash_vouchers"]["count"],
        payload["bank_vouchers"]["count"],
    )
    return payload


UNREADABLE_COMPANY_HINT = (
    "Tally answered but returned no company data -- the company isn't loaded, or "
    "its shared data folder (\\\\accounts\\D\\Tally.ERP9_GST\\Data) can't be read "
    "from this PC. Nothing was written to Firestore."
)


def _refuse_unreadable_company(cash_ledgers, bank_ledgers):
    """Raise before anything is fetched or written when Tally is up but can't
    see its own company data. Confirmed against a real run (2 Oct 2026): with
    the shared data folder unreachable, Tally still answered every request --
    with empty lists, not an error -- and both ledger lookups came back empty.
    Carrying on from there would write Rs.0 sales/purchase/cash/bank over a
    real day (or, in a backfill, over every day in the range). That run only
    escaped because the next request, the whole-year voucher fetch, crashed
    Tally outright; it's checked here so it doesn't rely on that again.

    This company has ledgers under both Cash-in-Hand and Bank Accounts, so
    both lists empty at once can only mean Tally can't read the company.
    """
    if not cash_ledgers and not bank_ledgers:
        raise TallyError("No Cash-in-Hand and no Bank ledgers at all. " + UNREADABLE_COMPANY_HINT)


def run(date, dry_run=False, dump_raw_dir=None):
    date_iso = date.strftime("%Y-%m-%d")
    log.info("Syncing %s for %s", TALLY_COMPANY_NAME, date_iso)

    voucher_type_parents = fetch_voucher_type_parents(date, dump_raw_dir)
    cash_ledgers = fetch_cash_ledger_names(date, dump_raw_dir)
    bank_ledgers = fetch_bank_ledger_names(date, dump_raw_dir)
    _refuse_unreadable_company(cash_ledgers, bank_ledgers)
    if not cash_ledgers:
        log.warning("No ledger found under 'Cash-in-Hand' -- cash voucher list will be empty. "
                    "Check the group name matches your Tally chart of accounts.")
    if not bank_ledgers:
        log.warning("No ledger found under 'Bank Accounts'/'Bank OD A/c' -- bank voucher list will be empty. "
                    "Check the group name matches your Tally chart of accounts.")
    # Fetched once and reused for both today's payload and the full
    # Delivery Challan list below -- Tally returns its whole voucher
    # history regardless of date (see _fetch_all_voucher_records), so a
    # second fetch here would just be the same slow request run twice.
    all_vouchers = _fetch_all_voucher_records(date, dump_raw_dir)
    if not all_vouchers:
        raise TallyError("No vouchers at all in the whole financial year. " + UNREADABLE_COMPANY_HINT)
    wanted = _fmt_date(date)
    vouchers = [v for v in all_vouchers if _text(v, "DATE") == wanted]
    balances = _GroupBalances(dump_raw_dir)

    payload = _build_payload(date, vouchers, voucher_type_parents, cash_ledgers, bank_ledgers, dump_raw_dir, balances)
    delivery_challans = _delivery_challan_records(all_vouchers, voucher_type_parents)
    proformas = _pending_proforma_records(all_vouchers)

    if dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        print(f"\n{len(delivery_challans)} Delivery Note voucher(s) found (not written, dry run):")
        print(json.dumps(delivery_challans, indent=2, ensure_ascii=False))
        print(f"\n{len(proformas)} Proforma Invoice(s) found (not written, dry run):")
        print(json.dumps(proformas, indent=2, ensure_ascii=False))
        sync_period_reports(date, date, dry_run=True, dump_raw_dir=dump_raw_dir)
        log.info("Dry run -- nothing written to Firestore.")
        return

    push_to_firestore(date_iso, payload)
    removed = push_delivery_challans_to_firestore(delivery_challans, _earliest_voucher_date(all_vouchers))
    listed = push_pending_proformas(proformas, _earliest_voucher_date(all_vouchers))
    push_serial_index(all_vouchers, voucher_type_parents)
    log.info("Written to Firestore: %s/%s (+%d delivery challans upserted, %d no longer in Tally removed; "
              "%d pending proforma invoices)",
              FIRESTORE_COLLECTION, date_iso, len(delivery_challans), removed, listed)
    sync_period_reports(date, date, dump_raw_dir=dump_raw_dir)
    _sync_party_balances(balances, vouchers=all_vouchers)
    # Two Stock Summary requests of 0.1-0.2 s each, and _sync_stock asks at
    # most every STOCK_MIN_HOURS however often this runs.
    if STOCK_ENABLED:
        _sync_stock(date, (payload.get("profit_and_loss") or {}).get("closing_stock"), dump_raw_dir=dump_raw_dir,
                    vouchers=all_vouchers, type_parents=voucher_type_parents)


def run_backfill(from_date, to_date, dry_run=False, dump_raw_dir=None):
    """Syncs every day from from_date to to_date (inclusive) in one run.
    Fetches the voucher list, voucher type classifications, and cash/bank
    ledgers only ONCE for the whole range (not once per day) -- P&L still
    needs one Tally request per day, since it's Tally's own per-day report
    export, but everything else is derived in Python from data already in
    hand. A failure on one day is logged and skipped rather than aborting
    the whole backfill, and there's a short pause between days so this
    doesn't hammer Tally with back-to-back requests.
    """
    day_count = (to_date - from_date).days + 1
    log.info("Backfilling %s from %s to %s (%d days)",
              TALLY_COMPANY_NAME, from_date, to_date, day_count)

    voucher_type_parents = fetch_voucher_type_parents(from_date, dump_raw_dir)
    cash_ledgers = fetch_cash_ledger_names(from_date, dump_raw_dir)
    bank_ledgers = fetch_bank_ledger_names(from_date, dump_raw_dir)
    _refuse_unreadable_company(cash_ledgers, bank_ledgers)
    if not cash_ledgers:
        log.warning("No ledger found under 'Cash-in-Hand' -- cash voucher lists will be empty. "
                    "Check the group name matches your Tally chart of accounts.")
    if not bank_ledgers:
        log.warning("No ledger found under 'Bank Accounts'/'Bank OD A/c' -- bank voucher lists will be empty. "
                    "Check the group name matches your Tally chart of accounts.")
    vouchers_by_date = fetch_vouchers_grouped_by_date(dump_raw_dir)
    if not vouchers_by_date:
        raise TallyError("No vouchers at all in the whole financial year. " + UNREADABLE_COMPANY_HINT)

    all_vouchers = [v for day_vouchers in vouchers_by_date.values() for v in day_vouchers]
    balances = _GroupBalances(dump_raw_dir)

    succeeded = 0
    failed = []
    cur = from_date
    while cur <= to_date:
        date_str = cur.strftime("%Y-%m-%d")
        try:
            vouchers = vouchers_by_date.get(_fmt_date(cur), [])
            payload = _build_payload(cur, vouchers, voucher_type_parents, cash_ledgers, bank_ledgers, dump_raw_dir,
                                     balances)
            if not dry_run:
                push_to_firestore(date_str, payload)
            succeeded += 1
        except TallyError as e:
            log.error("%s: skipped -- %s", date_str, e)
            failed.append(date_str)
        cur += datetime.timedelta(days=1)
        if cur <= to_date:
            time.sleep(2)

    # Delivery Challans aren't date-scoped (see _delivery_challan_records),
    # so this is derived from the whole fetch above and written once for
    # the whole backfill, not once per day.
    delivery_challans = _delivery_challan_records(all_vouchers, voucher_type_parents)
    proformas = _pending_proforma_records(all_vouchers)
    removed = 0
    listed = len(proformas)
    if not dry_run:
        removed = push_delivery_challans_to_firestore(delivery_challans, _earliest_voucher_date(all_vouchers))
        listed = push_pending_proformas(proformas, _earliest_voucher_date(all_vouchers))
        push_serial_index(all_vouchers, voucher_type_parents)

    log.info("Backfill done: %d/%d days written%s. %d delivery challans upserted, %d no longer in Tally removed. "
              "%d pending proforma invoices.",
              succeeded, day_count, f", {len(failed)} failed ({', '.join(failed)})" if failed else "",
              len(delivery_challans), removed, listed)
    sync_period_reports(from_date, to_date, dry_run=dry_run, dump_raw_dir=dump_raw_dir)
    _sync_party_balances(balances, dry_run=dry_run, force=True, vouchers=all_vouchers)
    # As of today, like the party balances; the backfill's last day is
    # yesterday, so there's no P&L for today at hand to check it against.
    if STOCK_ENABLED:
        _sync_stock(datetime.date.today(), None, dry_run=dry_run, dump_raw_dir=dump_raw_dir,
                    vouchers=all_vouchers, type_parents=voucher_type_parents)


# ---------------------------------------------------------------------------
# One sync at a time, and the Sync now listener
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


@contextlib.contextmanager
def _exclusive_lock(name, wait_seconds):
    """Holds an OS file lock in this folder for the duration, waiting up to
    wait_seconds for whoever has it. The OS drops the lock if the holder
    dies, so a crashed run never leaves it stuck.

    Two syncs used to be impossible -- one task, once a day. With the
    listener's hourly and on-demand syncs alongside the 10 AM run they can
    overlap, and Tally has hung here before under nothing worse than one
    slow request, so a second sync waits for the first instead.
    """
    handle = open(os.path.join(SCRIPT_DIR, name), "a+")
    deadline = time.time() + wait_seconds
    while True:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() >= deadline:
                handle.close()
                raise TallyError(f"Another sync is still running ({name} is locked).")
            time.sleep(5)
    try:
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.close()


def _utc_now():
    # Stored in UTC with the zone attached, so the dashboard shows the right
    # time wherever it's opened -- the owner checks it from abroad.
    return datetime.datetime.now(datetime.timezone.utc)


LISTENER_POLL_SECONDS = 20
LISTENER_HEARTBEAT_SECONDS = 300
# A press older than this was made while this PC was off; syncing for it
# whenever the PC next starts would only surprise someone.
LISTENER_STALE_REQUEST_MINUTES = 30

# On 8 Oct 2026 the owner was out, the laptop was off, and that day was never
# synced: Sync now only syncs today, and the 10 AM run that re-syncs the
# last week was closed when its window came up the next morning. So each
# sync the listener runs first looks at the last CATCH_UP_DAYS days and, if
# any is missing or was last synced before it ended (entries made after
# that aren't in it), syncs from the earliest such day to today in one go.
CATCH_UP_DAYS = 7
CATCH_UP_FROM_HOUR = 11
# A day Tally can't give (it fails every time) stays behind; this keeps the
# retry to every few hours instead of every sync. Sync now always retries.
CATCH_UP_EVERY_HOURS = 3


def _days_behind(db, today):
    """The days of the last CATCH_UP_DAYS before today, oldest first, that
    aren't in daily_reports or were last synced on the day itself. synced_at
    is the PC's own clock, India time, as YYYY-MM-DDTHH:MM:SS, so a plain
    string comparison with the next day's date tells which."""
    behind = []
    for n in range(CATCH_UP_DAYS, 0, -1):
        day = today - datetime.timedelta(days=n)
        snap = db.collection(FIRESTORE_COLLECTION).document(day.isoformat()).get()
        synced = str(((snap.to_dict() or {}) if snap.exists else {}).get("synced_at") or "")
        if synced < (day + datetime.timedelta(days=1)).isoformat():
            behind.append(day)
    return behind


def _sync_role():
    """"primary" on the main sync PC, "backup" anywhere else -- see
    AGENT_STALE_MINUTES."""
    try:
        with open(os.path.join(SCRIPT_DIR, "sync_role.txt")) as f:
            return "primary" if f.read().strip().lower() == "primary" else "backup"
    except OSError:
        return "backup"


def _agent_doc_id(host):
    return "agent_" + re.sub(r"[^A-Za-z0-9_.-]", "_", host)


def _primary_elsewhere(control, host, now_utc):
    """The name of another PC that is the main sync PC and has checked in
    within AGENT_STALE_MINUTES, or None."""
    for doc in control.stream():
        if not doc.id.startswith("agent_"):
            continue
        d = doc.to_dict() or {}
        if d.get("role") != "primary" or d.get("host") == host:
            continue
        try:
            seen = datetime.datetime.fromisoformat(d.get("seen_at") or "")
        except ValueError:
            continue
        if now_utc - seen <= datetime.timedelta(minutes=AGENT_STALE_MINUTES):
            return d.get("host")
    return None


class _TallyChangeWatch:
    """Tells the listener when anything has been saved in Tally, by asking
    Tally for the company's alteration counters (AltVchId, AltMstId) --
    one small request every CHANGE_POLL_SECONDS. Called with no useful
    argument; returns a token that changes when Tally's data does, or None
    when it can't tell (then the listener falls back to the hourly sync).

    It used to scan Tally's data folder over the network instead. On
    5 Oct 2026 Tally froze the moment the listener first started doing
    that -- on a request that had worked for weeks -- and while that can't
    be proved to be the cause, nothing that reads Tally's own files from
    outside Tally is worth the risk. This only talks to Tally the way
    every other request does, and never while a sync holds sync.lock, so
    it can't land on Tally in the middle of a sync."""

    def __init__(self):
        self.token = None
        self.last_poll = 0.0
        self.said_unavailable = False

    def __call__(self, _unused=None):
        if self.last_poll and time.time() - self.last_poll < CHANGE_POLL_SECONDS:
            return self.token
        self.last_poll = time.time()
        today = datetime.date.today()
        try:
            with _exclusive_lock("sync.lock", 0):
                root = _post_xml(_collection_request("CmpAlterIds", "Company", ["NAME", "ALTVCHID", "ALTMSTID"],
                                                     today, today), timeout=20, max_attempts=1)
        except TallyError:
            return self.token          # a sync is running, or Tally is busy: keep what we had
        for cmp in _collection_records(root, "COMPANY"):
            name = (cmp.get("NAME") or _text(cmp, "NAME") or "").strip()
            vch, mst = _text(cmp, "ALTVCHID"), _text(cmp, "ALTMSTID")
            if name.lower() == TALLY_COMPANY_NAME.lower() and (vch or mst):
                self.token = f"{vch}/{mst}"
                return self.token
        if not self.said_unavailable:
            log.info("Tally didn't give its alteration counters -- syncing hourly only.")
            self.said_unavailable = True
        return self.token


class _Listener:
    """The state run_listener carries between polls. Each tick reads the
    request document once and acts on at most one thing, so it can be
    driven and tested one poll at a time.

    What makes it sync, in order: a Sync now press; Tally's data having
    changed and then stayed quiet for CHANGE_QUIET_SECONDS (at most every
    CHANGE_MIN_GAP_MINUTES); the hourly sync during office hours, as a
    fallback when changes can't be seen. All of it only on the PC whose
    turn it is -- see _primary_elsewhere."""

    def __init__(self, db, run_sync, role=None, data_dir=None, newest_change=None):
        self.db = db
        self.control = db.collection(SYNC_CONTROL_COLLECTION)
        self.run_sync = run_sync
        self.host = _host_name()
        self.role = role or _sync_role()
        self.data_dir = data_dir
        self.newest_change = newest_change or _TallyChangeWatch()
        status = self.control.document("status").get()
        status = (status.to_dict() or {}) if status.exists else {}
        self.handled_request_id = status.get("handled_request_id")
        self.last_hourly_slot = status.get("last_hourly_slot")
        self.last_beat = 0.0
        self.active = self.role == "primary"
        self.last_sync = 0.0
        self.last_catch_up = 0.0
        # Whatever Tally's counters say at start-up counts as synced; a
        # change from here on is what triggers a sync.
        self.synced_change = self.newest_change(self.data_dir)
        self.seen_change, self.seen_change_at = self.synced_change, 0.0

    def _status(self, fields):
        self.control.document("status").set(fields, merge=True)

    def _heartbeat(self, now_utc):
        """Every few minutes: say this PC is alive, and work out whether
        it's this PC's turn to sync."""
        self.control.document(_agent_doc_id(self.host)).set(
            {"host": self.host, "role": self.role, "seen_at": now_utc.isoformat(), "active": self.active})
        was_active = self.active
        other = None if self.role == "primary" else _primary_elsewhere(self.control, self.host, now_utc)
        self.active = other is None
        if self.active != was_active:
            log.info("%s -- %s", "Main sync PC is quiet, this backup PC takes over" if self.active
                     else f"Main sync PC {other} is back, this backup PC stands by", self.host)
        if self.active:
            self._status({"listener_seen_at": now_utc.isoformat(), "host": self.host, "role": self.role})

    def tick(self, now_local, now_utc):
        if time.time() - self.last_beat >= LISTENER_HEARTBEAT_SECONDS:
            self._heartbeat(now_utc)
            self.last_beat = time.time()

        request = self.control.document("request").get()
        request = (request.to_dict() or {}) if request.exists else {}
        request_id = request.get("request_id")
        if request_id and request_id != self.handled_request_id:
            self.handled_request_id = request_id
            if not self.active:
                return      # the main PC answers it
            requested_at = request.get("requested_at")
            fresh = (requested_at is not None and
                     now_utc - requested_at <= datetime.timedelta(minutes=LISTENER_STALE_REQUEST_MINUTES))
            if fresh:
                log.info("Sync now pressed by %s", request.get("requested_by") or "the dashboard")
                self._sync("button", {"handled_request_id": request_id})
            else:
                log.info("Ignoring a Sync now press from %s -- older than %d minutes",
                          requested_at, LISTENER_STALE_REQUEST_MINUTES)
                self._status({"handled_request_id": request_id})
            return

        newest = self.newest_change(self.data_dir)
        if newest is not None and newest != self.seen_change:
            self.seen_change, self.seen_change_at = newest, time.time()
        if not self.active:
            return
        if (self.seen_change is not None and self.seen_change != self.synced_change
                and time.time() - self.seen_change_at >= CHANGE_QUIET_SECONDS
                and time.time() - self.last_sync >= CHANGE_MIN_GAP_MINUTES * 60):
            self.synced_change = self.seen_change
            self._sync("change", {}, now_local)
            return

        slot = now_local.strftime("%Y-%m-%dT%H")
        if (HOURLY_SYNC_FROM_HOUR <= now_local.hour <= HOURLY_SYNC_TO_HOUR
                and slot != self.last_hourly_slot):
            self.last_hourly_slot = slot
            self._sync("hourly", {"last_hourly_slot": slot}, now_local)

    def _sync(self, trigger, extra, now_local=None):
        self.last_sync = time.time()
        # A day missed while this PC was off is filled in by the same sync
        # (see _days_behind) -- after CATCH_UP_FROM_HOUR, so it doesn't race
        # the 10 AM run doing the same thing, or straight away when Sync now
        # is pressed.
        behind = []
        if trigger == "button" or ((now_local or datetime.datetime.now()).hour >= CATCH_UP_FROM_HOUR
                                   and time.time() - self.last_catch_up >= CATCH_UP_EVERY_HOURS * 3600):
            try:
                behind = _days_behind(self.db, datetime.date.today())
            except Exception as e:
                log.warning("Couldn't check the last %d days for gaps: %s", CATCH_UP_DAYS, e)
        if behind:
            self.last_catch_up = time.time()
            log.info("Days missed or synced before they ended: %s -- syncing from %s to today",
                     ", ".join(d.isoformat() for d in behind), behind[0])
        self._status({**extra, "state": "running", "trigger": trigger, "host": self.host,
                      "started_at": _utc_now().isoformat(),
                      "catching_up_from": behind[0].isoformat() if behind else None})
        ok, message = self.run_sync(behind[0]) if behind else self.run_sync()
        self._status({"state": "idle", "ok": ok, "message": message, "trigger": trigger,
                      "finished_at": _utc_now().isoformat()})
        log.info("%s sync %s%s", trigger, "done" if ok else "FAILED", f" -- {message}" if message else "")


def _sync_today_in_subprocess(catch_up_from=None):
    """Runs "sync_tally.py" for today as its own process, so every sync uses
    the copy on disk -- which the 10 AM run keeps up to date from GitHub --
    rather than whatever version this long-running listener started with.
    With catch_up_from, every day from then to today instead (a backfill),
    for the days _days_behind found. Returns (ok, message), the message
    being the last error line, worded for the dashboard."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    extra = ["--backfill-from", catch_up_from.isoformat()] if catch_up_from else []
    try:
        proc = subprocess.run([sys.executable, os.path.abspath(__file__)] + extra, cwd=SCRIPT_DIR,
                              capture_output=True, text=True, timeout=45 * 60, creationflags=flags)
    except subprocess.TimeoutExpired:
        return False, "The sync took over 45 minutes and was stopped -- Tally may have hung."
    output = (proc.stdout or "") + (proc.stderr or "")
    for line in output.splitlines():
        log.info("  | %s", line)
    if proc.returncode == 0:
        return True, ""
    errors = [line.split("] ", 1)[-1] for line in output.splitlines() if "[ERROR]" in line]
    message = errors[-1] if errors else f"The sync stopped with exit code {proc.returncode}."
    if "Could not reach Tally" in message or "did not respond" in message:
        message = f"Tally is not open on {_host_name()}, or not answering. " + message
    return False, message[:400]


def _leave_own_console():
    """Closes the console window Windows opens when this is started with
    python.exe rather than pythonw.exe -- on the owner's laptop a black
    "C:\\Users\\...\\python.exe" window popped up in front of his work,
    first when the task (re)started the listener and then, on 7 Oct 2026 at
    5:20 PM, for a whole sync of today with every Tally request scrolling
    past. Only a console this process has to itself is left: one someone is
    typing in (sync_tally.py --check run by hand from PowerShell), or the
    daily task's own cmd, has the shell attached too and stays as it is.
    Output then goes nowhere, so the caller logs to a file instead. Returns
    whether it left one."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        attached = (ctypes.c_uint * 4)()
        if kernel32.GetConsoleWindow() and kernel32.GetConsoleProcessList(attached, 4) == 1:
            kernel32.FreeConsole()
            sys.stdout = sys.stderr = open(os.devnull, "w")
            return True
    except Exception:
        pass
    return False


def _parent_program():
    """The program that started this one ("C:\\...\\svchost.exe" for a
    scheduled task, "...\\pythonw.exe" for the listener, "...\\explorer.exe"
    for a double-click), for the log when a run arrived with a window of
    its own -- so the next one can be traced to whatever started it."""
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, os.getppid())   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return f"process {os.getppid()}"
        name, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
        ok = kernel32.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size))
        kernel32.CloseHandle(handle)
        return name.value if ok else f"process {os.getppid()}"
    except Exception:
        return "unknown"


def run_listener():
    """Waits for the dashboard's Sync now button and syncs today when it's
    pressed, and also syncs today once an hour between HOURLY_SYNC_FROM_HOUR
    and HOURLY_SYNC_TO_HOUR. run_daily_sync.ps1 registers a scheduled task
    that starts this at logon and restarts it every ten minutes if it isn't
    running (only one copy runs: the second one finds the lock taken and
    exits).

    Without it the dashboard only had today's figures when someone ran a
    sync by hand at the Tally PC: the 10 AM run syncs up to yesterday.

    The dashboard can't reach Tally itself -- it's a website, and Tally is on
    a PC in the office -- so the button only writes a request document, and
    this polls for it every LISTENER_POLL_SECONDS. Polling rather than a
    live Firestore listener because it simply picks up again after the
    office internet drops, and costs about 4,300 reads a day, well inside
    the free allowance. It exits when sync_tally.py on disk changes, so the
    restart picks up the new version.
    """
    try:
        lock = _exclusive_lock("listener.lock", 0)
        lock.__enter__()
    except TallyError:
        log.info("The listener is already running on this PC.")
        return 0
    started_version = os.path.getmtime(os.path.abspath(__file__))
    log.info("Listening for Sync now on %s (and syncing today hourly, %d:00-%d:59)",
             _host_name(), HOURLY_SYNC_FROM_HOUR, HOURLY_SYNC_TO_HOUR)
    listener = None
    while True:
        try:
            if listener is None:
                listener = _Listener(_firestore_db(), _sync_today_in_subprocess)
            listener.tick(datetime.datetime.now(), _utc_now())
        except Exception as e:
            # Most likely the office internet dropped; the next poll retries.
            log.warning("Listener poll failed: %s", e)
        if os.path.getmtime(os.path.abspath(__file__)) != started_version:
            log.info("sync_tally.py was updated -- exiting so the scheduled task restarts the new version.")
            return 0
        time.sleep(LISTENER_POLL_SECONDS)


def _record_result(ok, message, dry_run=False):
    """Notes every sync's outcome -- the scheduled one, the listener's, one
    run by hand -- where the dashboard and the "sync has stopped" alert
    (.github/scripts/sync_alert.py) read it. Best effort: never turns a
    good sync into a failed one."""
    if dry_run:
        return
    now = _utc_now().isoformat()
    fields = ({"last_ok_at": now, "last_ok_host": _host_name()} if ok
              else {"last_error_at": now, "last_error_host": _host_name(), "last_error": message[:400]})
    try:
        _firestore_db().collection(SYNC_CONTROL_COLLECTION).document("status").set(fields, merge=True)
    except Exception as e:
        log.warning("Sync outcome not recorded -- %s", e)


def run_test_bills():
    """--test-bills: asks Tally for its Bills Receivable and Bills Payable as
    at today and prints how long each took, how many bills are pending,
    their total next to the Sundry Debtors/Creditors total, and the parties
    with the oldest unpaid bills -- writing nothing to the dashboard. Run
    by hand while watching Tally before any sync relies on it."""
    today = datetime.date.today()
    dump = os.path.join(SCRIPT_DIR, "tally_test_output")
    rupees = lambda n: f"Rs.{n:,.2f}"
    with _exclusive_lock("sync.lock", 60):
        for kind, group, sign in (("receivable", "Sundry Debtors", 1), ("payable", "Sundry Creditors", -1)):
            started = time.time()
            bills = fetch_bills(kind, today, dump)
            took = time.time() - started
            group_total = sign * sum(line["closing"] for line in fetch_group_summary(group, today, dump))
            owed = sum(b["amount"] for b in bills)
            parties = oldest_bills_by_party(bills, today)
            print(f"\nBills {kind} as at {today:%d %b %Y}: {len(bills)} bills of {len(parties)} parties, "
                  f"Tally answered in {took:.1f} s")
            print(f"    {'Pending bills total':<44} {rupees(owed):>18}")
            print(f"    {group + ' total (should be close)':<44} {rupees(group_total):>18}")
            if not bills:
                raw = os.path.join(dump, f"bills_{kind}.xml")
                if os.path.exists(raw):
                    with open(raw, encoding="utf-8") as f:
                        print("    No bills read -- Tally's answer, as sent:\n" + f.read()[:1200])
            print("    Oldest unpaid, by party:")
            for name, p in sorted(parties.items(), key=lambda kv: -kv[1]["days"])[:12]:
                print(f"      {name[:40]:<40} since {p['oldest']}  {p['days']:>5} days  ({p['bills']} bills)")
    print(f"\nRaw answers saved in {dump}. Nothing was written to the dashboard.")
    return 0


def run_test_group(group):
    """--test-group "Cash-in-Hand": asks Tally for its Group Summary of one
    group as at today and prints each line and the total, to compare with
    the same group in Tally -- writing nothing to the dashboard. Run by hand
    while watching Tally, smallest group first, before any sync relies on
    it. The raw answer is saved in tally_test_output for checking."""
    today = datetime.date.today()
    with _exclusive_lock("sync.lock", 60):
        started = time.time()
        lines = fetch_group_summary(group, today, dump_raw_dir=os.path.join(SCRIPT_DIR, "tally_test_output"))
        took = time.time() - started
    rupees = lambda n: f"Rs.{n:,.2f}"
    print(f"\n{group} as at {today:%d %b %Y}: {len(lines)} lines, Tally answered in {took:.1f} s\n")
    for line in lines[:40]:
        print(f"    {line['name']:<45} {rupees(line['closing']):>20}")
    if len(lines) > 40:
        print(f"    ... and {len(lines) - 40} more")
    print(f"\n    {'Total (debit positive)':<45} {rupees(sum(l['closing'] for l in lines)):>20}")
    print("\nNothing was written to the dashboard.")
    return 0


def run_test_stock(items=False, categories=False):
    """--test-stock: asks Tally for its Stock Summary as at today and prints
    each line and the total next to Tally's own P&L closing stock, which
    should be the same figure -- writing nothing to the dashboard. Run by
    hand while watching Tally before any sync relies on it. The raw answer
    is saved in tally_test_output for checking."""
    today = datetime.date.today()
    dump = os.path.join(SCRIPT_DIR, "tally_test_output")
    with _exclusive_lock("sync.lock", 60):
        started = time.time()
        checks = []
        if categories:
            try:
                total, lines = fetch_stock_by_category(today, dump, report_checks=checks)
            except StockShapeError as e:
                print(f"\nStock Category Summary can't be used as it is: {e}")
                lines, total = [], None
        else:
            lines = fetch_stock_summary(today, dump_raw_dir=dump, items=items)
        took = time.time() - started
        try:
            pl_stock = fetch_profit_and_loss(today, dump).get("closing_stock")
        except TallyError as e:
            pl_stock = None
            log.warning("P&L for comparison not read -- %s", e)
    rupees = lambda n: f"Rs.{n:,.2f}"
    for label, count, rows in checks:
        print(f"\nCategory check, {label} report ({count} lines):")
        for c in rows[:25]:
            print(f"    {'OK ' if c['ok'] else 'NO '} {c['name'][:34]:<34} line {rupees(c['value']):>16}"
                  f"   {c['items']:>4} items {rupees(c['items_value']):>16}")
        if len(rows) > 25:
            print(f"    ... and {len(rows) - 25} more categories")
    name = "Stock Category Summary, item by item" if categories else "Stock Summary"
    print(f"\n{name} as at {today:%d %b %Y}: {len(lines)} lines, Tally answered in {took:.1f} s\n")
    for line in lines[:40]:
        where = f"[{line['group'] or 'no category'}] " if categories else ""
        print(f"    {(where + line['name'])[:52]:<52} {line['qty_text']:>10} {line.get('rate_text', ''):>12} {rupees(line['value']):>16}")
    if len(lines) > 40:
        print(f"    ... and {len(lines) - 40} more")
    if not categories or total is None:
        total = sum(l["value"] for l in lines)
    print(f"\n    {'Total':<52} {'':>10} {'':>12} {rupees(total):>16}")
    if pl_stock is not None:
        print(f"    {'P&L closing stock':<52} {'':>10} {'':>12} {rupees(pl_stock):>16}")
    raw = os.path.join(dump, ("stock_category_summary_items" if categories else
                              "stock_summary_items" if items else "stock_summary") + ".xml")
    if len(lines) < 3 and not categories and os.path.exists(raw):
        with open(raw, encoding="utf-8") as f:
            print("\nTally's answer, as sent:\n" + f.read()[:1500])
    print(f"\nRaw answer saved in {dump}. Nothing was written to the dashboard.")
    return 0


def run_check():
    """Checks the two things a sync needs, the same way a sync uses them,
    and prints one CHECK line for each: the Firebase key (reads from the
    database) and Tally (the ledger lookups a sync starts with, through the
    same requests code and the same unreadable-company guard). Returns 0
    when both pass. Setup-Tally-Sync.cmd runs this to tell whoever is
    setting up an office PC exactly what still needs fixing.

    Tally is checked through this code rather than a separate web request
    on purpose: a check that takes a different network path -- PowerShell's
    can go through a proxy that Python's doesn't -- can fail while the sync
    would work, or pass while it wouldn't.
    """
    global REQUEST_TIMEOUT_SECONDS
    ok = True
    try:
        list(_firestore_db().collection(FIRESTORE_COLLECTION).limit(1).stream())
        print("CHECK database: OK")
    except Exception as e:
        print(f"CHECK database: FAILED -- {e}")
        ok = False
    # A stuck Tally shouldn't keep a setup screen waiting the sync's two minutes.
    REQUEST_TIMEOUT_SECONDS = 20
    try:
        today = datetime.date.today()
        cash, bank = fetch_cash_ledger_names(today), fetch_bank_ledger_names(today)
        _refuse_unreadable_company(cash, bank)
        print(f"CHECK tally: OK -- {TALLY_COMPANY_NAME} is readable ({len(cash)} cash, {len(bank)} bank ledgers)")
    except TallyError as e:
        print(f"CHECK tally: FAILED -- {e}")
        ok = False
    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="YYYY-MM-DD, defaults to today", default=None)
    parser.add_argument("--backfill-from", help="YYYY-MM-DD -- sync every day from this date to --backfill-to (or today) in one run", default=None)
    parser.add_argument("--backfill-to", help="YYYY-MM-DD, defaults to today. Only used with --backfill-from", default=None)
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print, do not write to Firestore")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dump-raw-dir", default=None, help="Save every raw Tally XML response here for debugging")
    parser.add_argument("--check", action="store_true",
                        help="Only check that the Firebase key works and Tally answers with the company readable, then exit")
    parser.add_argument("--test-group", metavar="GROUP",
                        help="Print Tally's Group Summary of one group as at today, to compare with Tally, write nothing")
    parser.add_argument("--test-stock", nargs="?", const="summary", choices=["summary", "items", "categories"],
                        help="Print Tally's Stock Summary as at today next to the P&L closing stock, write nothing; "
                             "'items' asks for it exploded, 'categories' for the Stock Category Summary item by item")
    parser.add_argument("--test-bills", action="store_true",
                        help="Print Tally's Bills Receivable and Bills Payable as at today, write nothing")
    parser.add_argument("--if-leader", action="store_true",
                        help="Skip (exit code 3) when this is the backup PC and the main sync PC is active")
    parser.add_argument("--import-serials", action="store_true",
                        help="Once: read the serial numbers of the earlier years' companies open in Tally into the Serial No. Search")
    parser.add_argument("--listen", action="store_true",
                        help="Keep running: sync today when the dashboard's Sync now button is pressed, and hourly")
    args = parser.parse_args()

    log_format = "%(asctime)s [%(levelname)s] %(message)s"
    if args.listen:
        # Started by pythonw, which has no console to log to. Capped at
        # about 4 MB in all, since it runs for months.
        logging.basicConfig(level=logging.INFO, format=log_format, handlers=[
            logging.handlers.RotatingFileHandler(os.path.join(SCRIPT_DIR, "sync_listener_log.txt"),
                                                 maxBytes=2_000_000, backupCount=1, encoding="utf-8")])
        _leave_own_console()
        sys.exit(run_listener())

    # A sync that arrived in a window of its own works in the background
    # like the listener: the window goes and the log goes to a file.
    if _leave_own_console():
        logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format=log_format, handlers=[
            logging.handlers.RotatingFileHandler(os.path.join(SCRIPT_DIR, "sync_run_log.txt"),
                                                 maxBytes=1_000_000, backupCount=1, encoding="utf-8")])
        log.info("Started in a window of its own by %s (%s) -- closed it, logging here instead.",
                 _parent_program(), " ".join(sys.argv))
    else:
        logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format=log_format)

    if args.check:
        sys.exit(run_check())

    if args.import_serials:
        try:
            with _exclusive_lock("sync.lock", 40 * 60):
                sys.exit(run_import_serials())
        except TallyError as e:
            log.error("Tally error: %s", e)
            sys.exit(1)

    if args.test_group:
        try:
            sys.exit(run_test_group(args.test_group))
        except TallyError as e:
            log.error("Tally error: %s", e)
            sys.exit(1)

    if args.test_bills:
        try:
            sys.exit(run_test_bills())
        except TallyError as e:
            log.error("Tally error: %s", e)
            sys.exit(1)

    if args.test_stock:
        try:
            sys.exit(run_test_stock(items=args.test_stock == "items", categories=args.test_stock == "categories"))
        except TallyError as e:
            log.error("Tally error: %s", e)
            sys.exit(1)

    if args.if_leader and _sync_role() != "primary":
        try:
            other = _primary_elsewhere(_firestore_db().collection(SYNC_CONTROL_COLLECTION), _host_name(), _utc_now())
        except Exception as e:
            other = None
            log.warning("Couldn't check for the main sync PC (%s) -- syncing from here.", e)
        if other:
            log.info("Main sync PC %s is active -- this backup PC skips the scheduled sync.", other)
            sys.exit(3)

    try:
        with _exclusive_lock("sync.lock", 40 * 60):
            if args.backfill_from:
                from_date = datetime.datetime.strptime(args.backfill_from, "%Y-%m-%d").date()
                to_date = (datetime.datetime.strptime(args.backfill_to, "%Y-%m-%d").date()
                           if args.backfill_to else datetime.date.today())
                run_backfill(from_date, to_date, dry_run=args.dry_run, dump_raw_dir=args.dump_raw_dir)
            else:
                date = (datetime.datetime.strptime(args.date, "%Y-%m-%d").date()
                        if args.date else datetime.date.today())
                run(date, dry_run=args.dry_run, dump_raw_dir=args.dump_raw_dir)
    except TallyError as e:
        log.error("Tally error: %s", e)
        _record_result(False, f"Tally error: {e}", args.dry_run)
        sys.exit(1)
    except Exception as e:
        log.error("Sync failed: %s", e, exc_info=args.verbose)
        _record_result(False, f"Sync failed: {e}", args.dry_run)
        sys.exit(1)
    if not args.dry_run:
        try:
            _move_pl_out_of_daily_reports()
        except Exception as e:
            log.warning("P&L not moved out of the daily documents yet -- %s", e)
    _record_result(True, "", args.dry_run)


if __name__ == "__main__":
    main()
