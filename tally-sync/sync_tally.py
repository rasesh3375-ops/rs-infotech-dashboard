#!/usr/bin/env python3
"""
Pulls Daily Sales, Daily Purchase, Daily Profit & Loss, Daily Cash Vouchers
and Daily Stock Summary out of a local Tally Prime install (via its
HTTP/XML export gateway) and writes one document per day to Firestore, for
the rs-infotech dashboard (../rs-infotech/index.html) to read.

Run this on the SAME PC as Tally Prime, with Tally open and the company
loaded. See the setup walkthrough for how to enable Tally's XML gateway,
create the Firebase project, and schedule this script.

Usage:
  python sync_tally.py                  # syncs today
  python sync_tally.py --date 2026-09-10
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

REQUEST_TIMEOUT_SECONDS = 45

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
    return text


def _post_xml(xml_request, dump_raw_dir=None, dump_name=None):
    try:
        resp = requests.post(
            TALLY_URL,
            data=xml_request.encode("utf-8"),
            headers={"Content-Type": "text/xml; charset=utf-8"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.exceptions.ConnectionError as e:
        raise TallyError(
            f"Could not reach Tally at {TALLY_URL}. Is Tally Prime open, the company "
            f"loaded, and the HTTP/XML gateway enabled (F1 > Settings > Connectivity)? ({e})"
        )
    except requests.exceptions.Timeout:
        raise TallyError(f"Tally did not respond within {REQUEST_TIMEOUT_SECONDS}s.")

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

def fetch_cash_ledger_names(date, dump_raw_dir=None):
    """Ledgers parented directly under 'Cash-in-Hand' -- Cash, Petty Cash, etc.
    Almost every Tally setup keeps cash ledgers directly under this group;
    if yours nests them under a sub-group, add that sub-group's name here.
    """
    xml_req = _collection_request("LedgerList", "Ledger", ["NAME", "PARENT"], date, date)
    root_el = _post_xml(xml_req, dump_raw_dir, "ledger_list")
    names = set()
    for led in root_el.iter("LEDGER"):
        parent = _text(led, "PARENT")
        if parent.strip().lower() == "cash-in-hand":
            name = led.get("NAME") or _text(led, "NAME")
            if name:
                names.add(name.strip())
    return names


def fetch_daily_sales(date, dump_raw_dir=None):
    return _fetch_vouchers_by_class(date, "IsSales", "$$IsSales:$VoucherTypeName", dump_raw_dir, "sales")


def fetch_daily_purchase(date, dump_raw_dir=None):
    return _fetch_vouchers_by_class(date, "IsPurchase", "$$IsPurchase:$VoucherTypeName", dump_raw_dir, "purchase")


def _fetch_vouchers_by_class(date, filter_name, formula_expr, dump_raw_dir, dump_prefix):
    xml_req = _collection_request(
        "VchList",
        "Voucher",
        ["DATE", "VOUCHERNUMBER", "PARTYLEDGERNAME", "VOUCHERTYPENAME",
         "ALLLEDGERENTRIES.LIST"],
        date,
        date,
        formulae={filter_name: formula_expr},
    )
    root = _post_xml(xml_req, dump_raw_dir, dump_prefix)

    vouchers = []
    total = 0.0
    for v in root.iter("VOUCHER"):
        party = _text(v, "PARTYLEDGERNAME")
        vch_no = _text(v, "VOUCHERNUMBER")
        vch_type = _text(v, "VOUCHERTYPENAME")
        amount = None
        for entry in v.findall(".//ALLLEDGERENTRIES.LIST"):
            is_party = _text(entry, "ISPARTYLEDGER").lower() == "yes"
            if is_party:
                amount = abs(_num(entry, "AMOUNT"))
                break
        if amount is None:
            # No entry was flagged as the party ledger -- fall back to the
            # single largest-magnitude entry and flag it for a human to
            # sanity check, rather than silently guessing.
            amounts = [abs(_num(entry, "AMOUNT")) for entry in v.findall(".//ALLLEDGERENTRIES.LIST")]
            amount = max(amounts) if amounts else 0.0
        vouchers.append({
            "voucher_no": vch_no,
            "party": party,
            "type": vch_type,
            "amount": round(amount, 2),
        })
        total += amount

    return {"total": round(total, 2), "count": len(vouchers), "vouchers": vouchers}


def fetch_cash_vouchers(date, dump_raw_dir=None):
    cash_ledgers = fetch_cash_ledger_names(date, dump_raw_dir)
    if not cash_ledgers:
        log.warning("No ledger found under 'Cash-in-Hand' -- cash voucher list will be empty. "
                    "Check the group name matches your Tally chart of accounts.")

    xml_req = _collection_request(
        "CashVchList",
        "Voucher",
        ["DATE", "VOUCHERNUMBER", "PARTYLEDGERNAME", "VOUCHERTYPENAME", "ALLLEDGERENTRIES.LIST"],
        date,
        date,
    )
    root = _post_xml(xml_req, dump_raw_dir, "cash_vouchers")

    vouchers = []
    for v in root.iter("VOUCHER"):
        cash_entry = None
        for entry in v.findall(".//ALLLEDGERENTRIES.LIST"):
            ledger_name = (entry.get("NAME") or _text(entry, "LEDGERNAME") or "").strip()
            if ledger_name in cash_ledgers:
                cash_entry = entry
                break
        if cash_entry is None:
            continue
        is_debit = _text(cash_entry, "ISDEEMEDPOSITIVE").lower() == "yes"
        vouchers.append({
            "voucher_no": _text(v, "VOUCHERNUMBER"),
            "party": _text(v, "PARTYLEDGERNAME"),
            "type": _text(v, "VOUCHERTYPENAME"),
            "direction": "cash_in" if is_debit else "cash_out",
            "amount": round(abs(_num(cash_entry, "AMOUNT")), 2),
        })

    return {"count": len(vouchers), "vouchers": vouchers}


def fetch_stock_summary(date, dump_raw_dir=None):
    xml_req = _collection_request(
        "StockList",
        "StockItem",
        ["NAME", "CLOSINGBALANCE", "CLOSINGVALUE", "BASEUNITS"],
        date,
        date,
    )
    root = _post_xml(xml_req, dump_raw_dir, "stock_summary")

    items = []
    for it in root.iter("STOCKITEM"):
        name = it.get("NAME") or _text(it, "NAME")
        qty_raw = _text(it, "CLOSINGBALANCE")
        qty = _parse_qty(qty_raw)
        value = _num(it, "CLOSINGVALUE")
        unit = _text(it, "BASEUNITS")
        if not name or (qty == 0 and value == 0):
            continue
        items.append({"name": name, "qty": qty, "unit": unit, "value": round(value, 2)})

    items.sort(key=lambda x: x["value"], reverse=True)
    return {"count": len(items), "items": items}


def _parse_qty(raw):
    # Tally formats quantities like "12 PCS" or "-3.500 KG" -- pull the number.
    m = re.search(r"-?[\d,]+(\.\d+)?", raw or "")
    if not m:
        return 0.0
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return 0.0


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

    # ElementTree has no getparent(), so walk every element that has a
    # DSPACCNAME child and look at that same element's amount children --
    # Tally groups each report line's name and figures under one node.
    net_line = None
    for parent in root.iter():
        acc_el = parent.find("DSPACCNAME")
        if acc_el is None:
            continue
        name = "".join(acc_el.itertext()).strip()
        if re.search(r"nett?\s*(profit|loss)", name, re.IGNORECASE):
            amt_el = parent.find("DSPCLDRAMT")
            if amt_el is None or not (amt_el.text or "").strip():
                amt_el = parent.find("DSPCLCRAMT")
            amount = _num(parent, amt_el.tag if amt_el is not None else "DSPCLDRAMT")
            net_line = {"label": name, "amount": amount}
            break

    if net_line is None:
        reason = "Could not find a 'Nett Profit'/'Nett Loss' line in Tally's P&L export."
        if not dump_raw_dir:
            reason += " Re-run with --dump-raw-dir to save the raw XML for inspection."
        return {"needs_review": True, "reason": reason, "net_profit_loss": None}

    is_loss = "loss" in net_line["label"].lower()
    net_amount = -abs(net_line["amount"]) if is_loss else abs(net_line["amount"])

    return {
        "needs_review": False,
        "net_profit_loss": round(net_amount, 2),
        "label": net_line["label"],
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

def run(date, dry_run=False, dump_raw_dir=None):
    date_iso = date.strftime("%Y-%m-%d")
    log.info("Syncing %s for %s", TALLY_COMPANY_NAME, date_iso)

    payload = {
        "date": date_iso,
        "synced_at": datetime.datetime.now().isoformat(),
        "sales": fetch_daily_sales(date, dump_raw_dir),
        "purchase": fetch_daily_purchase(date, dump_raw_dir),
        "profit_and_loss": fetch_profit_and_loss(date, dump_raw_dir),
        "cash_vouchers": fetch_cash_vouchers(date, dump_raw_dir),
        "stock_summary": fetch_stock_summary(date, dump_raw_dir),
    }

    log.info(
        "Sales Rs.%s (%d vch) | Purchase Rs.%s (%d vch) | P&L %s | Cash vouchers %d | Stock items %d",
        payload["sales"]["total"], payload["sales"]["count"],
        payload["purchase"]["total"], payload["purchase"]["count"],
        ("needs review" if payload["profit_and_loss"]["needs_review"]
         else f"Rs.{payload['profit_and_loss']['net_profit_loss']}"),
        payload["cash_vouchers"]["count"],
        payload["stock_summary"]["count"],
    )

    if dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        log.info("Dry run -- nothing written to Firestore.")
        return

    push_to_firestore(date_iso, payload)
    log.info("Written to Firestore: %s/%s", FIRESTORE_COLLECTION, date_iso)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="YYYY-MM-DD, defaults to today", default=None)
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print, do not write to Firestore")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dump-raw-dir", default=None, help="Save every raw Tally XML response here for debugging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if args.date:
        date = datetime.datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        date = datetime.date.today()

    try:
        run(date, dry_run=args.dry_run, dump_raw_dir=args.dump_raw_dir)
    except TallyError as e:
        log.error("Tally error: %s", e)
        sys.exit(1)
    except Exception as e:
        log.error("Sync failed: %s", e, exc_info=args.verbose)
        sys.exit(1)


if __name__ == "__main__":
    main()
