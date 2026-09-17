#!/usr/bin/env python3
"""
Pulls Daily Sales, Daily Purchase, Daily Profit & Loss and Daily Cash
Vouchers out of a local Tally Prime install (via its HTTP/XML export
gateway) and writes one document per day to Firestore, for the
rs-infotech dashboard (../rs-infotech/index.html) to read.

Stock Summary is deliberately NOT included here -- computing closing
stock balances/values as of a date was consistently the slowest thing
Tally did, and coincided with a real Tally Prime crash (Memory Access
Violation) on shared company data. Don't add it back without first
confirming with Tally support why that computation was unstable.

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
import datetime
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET

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

REQUEST_TIMEOUT_SECONDS = 120

log = logging.getLogger("tally_sync")


# ---------------------------------------------------------------------------
# Low-level Tally HTTP/XML plumbing
# ---------------------------------------------------------------------------

class TallyError(RuntimeError):
    pass


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
            last_error = TallyError(f"Tally did not respond within {timeout}s.")
        if attempt < max_attempts:
            log.warning("Attempt %d/%d failed (%s) -- retrying in 5s...", attempt, max_attempts, last_error)
            time.sleep(5)
    if resp is None:
        raise last_error

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


def _collection_request(collection_name, obj_type, fetch_fields, from_date, to_date, formulae=None):
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
    <SVCURRENTCOMPANY>{TALLY_COMPANY_NAME}</SVCURRENTCOMPANY>
    <SVFROMDATE>{_fmt_date(from_date)}</SVFROMDATE>
    <SVTODATE>{_fmt_date(to_date)}</SVTODATE>
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


def _fetch_all_voucher_records(anchor_date, dump_raw_dir=None):
    """One Tally request for every voucher it's willing to return -- empirically
    the whole financial year, since Tally's Voucher collection ignores
    SVFROMDATE/SVTODATE entirely. Shared by fetch_vouchers_for_date (a single
    day) and fetch_vouchers_grouped_by_date (a backfill across many days), so
    a backfill needs this request only once instead of once per day.
    """
    xml_req = _collection_request(
        "VchList",
        "Voucher",
        ["DATE", "VOUCHERNUMBER", "PARTYLEDGERNAME", "VOUCHERTYPENAME", "NARRATION",
         "ALLLEDGERENTRIES.LIST", "ALLINVENTORYENTRIES.LIST"],
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


def _filter_by_class(vouchers, type_parents, wanted_parent):
    result = []
    total = 0.0
    for v in vouchers:
        vch_type = _text(v, "VOUCHERTYPENAME")
        if type_parents.get(vch_type.strip().lower()) != wanted_parent:
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
            "direction": in_label if is_debit else out_label,
            "amount": round(abs(_num(matched_entry, "AMOUNT")), 2),
            "description": _voucher_description(v),
        })
    return {"count": len(result), "vouchers": result}


def _cash_vouchers_from(vouchers, cash_ledgers):
    return _ledger_touching_vouchers_from(vouchers, cash_ledgers, "cash_in", "cash_out")


def _bank_vouchers_from(vouchers, bank_ledgers):
    return _ledger_touching_vouchers_from(vouchers, bank_ledgers, "bank_in", "bank_out")


def fetch_profit_and_loss(date, dump_raw_dir=None):
    """Uses Tally's own native Profit & Loss report export for a single day
    (SVFROMDATE == SVTODATE == date), so Tally does the Income/Expense
    classification itself instead of us reimplementing it. The report's
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
     <SVTODATE>{_fmt_date(date)}</SVTODATE>
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
    # for this single day's net_profit_loss, and deliberately kept OUT of
    # total_income/total_expense, which stay flow-only and safe to sum
    # across any number of days.
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
    }


# ---------------------------------------------------------------------------
# Firestore
# ---------------------------------------------------------------------------

def push_to_firestore(date_iso, payload):
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

    db = firestore.client()
    db.collection(FIRESTORE_COLLECTION).document(date_iso).set(payload)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _build_payload(date, vouchers, voucher_type_parents, cash_ledgers, bank_ledgers, dump_raw_dir=None):
    payload = {
        "date": date.strftime("%Y-%m-%d"),
        "synced_at": datetime.datetime.now().isoformat(),
        "sales": _filter_by_class(vouchers, voucher_type_parents, "sales"),
        "purchase": _filter_by_class(vouchers, voucher_type_parents, "purchase"),
        "profit_and_loss": fetch_profit_and_loss(date, dump_raw_dir),
        "cash_vouchers": _cash_vouchers_from(vouchers, cash_ledgers),
        "bank_vouchers": _bank_vouchers_from(vouchers, bank_ledgers),
    }
    log.info(
        "%s: Sales Rs.%s (%d vch) | Purchase Rs.%s (%d vch) | P&L %s | Cash vouchers %d | Bank vouchers %d",
        payload["date"],
        payload["sales"]["total"], payload["sales"]["count"],
        payload["purchase"]["total"], payload["purchase"]["count"],
        ("needs review" if payload["profit_and_loss"]["needs_review"]
         else f"Rs.{payload['profit_and_loss']['net_profit_loss']}"),
        payload["cash_vouchers"]["count"],
        payload["bank_vouchers"]["count"],
    )
    return payload


def run(date, dry_run=False, dump_raw_dir=None):
    date_iso = date.strftime("%Y-%m-%d")
    log.info("Syncing %s for %s", TALLY_COMPANY_NAME, date_iso)

    voucher_type_parents = fetch_voucher_type_parents(date, dump_raw_dir)
    cash_ledgers = fetch_cash_ledger_names(date, dump_raw_dir)
    bank_ledgers = fetch_bank_ledger_names(date, dump_raw_dir)
    if not cash_ledgers:
        log.warning("No ledger found under 'Cash-in-Hand' -- cash voucher list will be empty. "
                    "Check the group name matches your Tally chart of accounts.")
    if not bank_ledgers:
        log.warning("No ledger found under 'Bank Accounts'/'Bank OD A/c' -- bank voucher list will be empty. "
                    "Check the group name matches your Tally chart of accounts.")
    vouchers = fetch_vouchers_for_date(date, dump_raw_dir)

    payload = _build_payload(date, vouchers, voucher_type_parents, cash_ledgers, bank_ledgers, dump_raw_dir)

    if dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        log.info("Dry run -- nothing written to Firestore.")
        return

    push_to_firestore(date_iso, payload)
    log.info("Written to Firestore: %s/%s", FIRESTORE_COLLECTION, date_iso)


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
    if not cash_ledgers:
        log.warning("No ledger found under 'Cash-in-Hand' -- cash voucher lists will be empty. "
                    "Check the group name matches your Tally chart of accounts.")
    if not bank_ledgers:
        log.warning("No ledger found under 'Bank Accounts'/'Bank OD A/c' -- bank voucher lists will be empty. "
                    "Check the group name matches your Tally chart of accounts.")
    vouchers_by_date = fetch_vouchers_grouped_by_date(dump_raw_dir)

    succeeded = 0
    failed = []
    cur = from_date
    while cur <= to_date:
        date_str = cur.strftime("%Y-%m-%d")
        try:
            vouchers = vouchers_by_date.get(_fmt_date(cur), [])
            payload = _build_payload(cur, vouchers, voucher_type_parents, cash_ledgers, bank_ledgers, dump_raw_dir)
            if not dry_run:
                push_to_firestore(date_str, payload)
            succeeded += 1
        except TallyError as e:
            log.error("%s: skipped -- %s", date_str, e)
            failed.append(date_str)
        cur += datetime.timedelta(days=1)
        if cur <= to_date:
            time.sleep(2)

    log.info("Backfill done: %d/%d days written%s.", succeeded, day_count,
              f", {len(failed)} failed ({', '.join(failed)})" if failed else "")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="YYYY-MM-DD, defaults to today", default=None)
    parser.add_argument("--backfill-from", help="YYYY-MM-DD -- sync every day from this date to --backfill-to (or today) in one run", default=None)
    parser.add_argument("--backfill-to", help="YYYY-MM-DD, defaults to today. Only used with --backfill-from", default=None)
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print, do not write to Firestore")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dump-raw-dir", default=None, help="Save every raw Tally XML response here for debugging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    try:
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
        sys.exit(1)
    except Exception as e:
        log.error("Sync failed: %s", e, exc_info=args.verbose)
        sys.exit(1)


if __name__ == "__main__":
    main()
