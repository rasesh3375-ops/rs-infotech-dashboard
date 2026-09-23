#!/usr/bin/env python3
"""
ONE-OFF DIAGNOSTIC -- not part of the real sync, delete this file once the
Pending Delivery Challan feature is built.

Reuses sync_tally.py's existing voucher fetch (which pulls Tally's whole
Voucher collection -- confirmed elsewhere in sync_tally.py that Tally
ignores SVFROMDATE/SVTODATE for this collection and returns everything
regardless) and reports, for every "Delivery Note" voucher, whether it
carries a TRACKINGNUMBER on any of its inventory entries -- the only
mechanism Tally has for later telling a billed Delivery Note apart from
one nothing has been invoiced against yet.

Also dumps the raw XML for the first few Delivery Note vouchers found,
and for any Sales voucher that shares a tracking number with one of them,
so the actual field shapes can be checked before writing real matching
logic -- same reasoning as every other "confirmed against real data"
comment already in sync_tally.py: guessing the field name wrong here
would silently mark every challan as pending forever, or the reverse.

Usage:
  python inspect_delivery_notes.py
  python inspect_delivery_notes.py --dump-raw-dir raw
"""

import argparse
import xml.etree.ElementTree as ET

import sync_tally as st


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dump-raw-dir", default=None, help="Also save the raw Tally XML response here")
    args = parser.parse_args()

    import datetime
    today = datetime.date.today()

    print(f"Fetching Tally's voucher type list to find the exact 'Delivery Note' type name(s)...")
    type_parents = st.fetch_voucher_type_parents(today, args.dump_raw_dir)
    delivery_types = [name for name, parent in type_parents.items() if parent == "delivery note"]
    sales_types = [name for name, parent in type_parents.items() if parent == "sales"]
    print(f"  Voucher types classified under 'Delivery Note': {delivery_types or '(none found)'}")
    print(f"  Voucher types classified under 'Sales': {sales_types or '(none found)'}")
    if not delivery_types:
        print("\nNo voucher type is classified under Tally's 'Delivery Note' base type.")
        print("All voucher type -> parent classifications Tally returned:")
        for name, parent in sorted(type_parents.items()):
            print(f"    {name!r} -> {parent!r}")
        return

    print(f"\nFetching every voucher Tally has (this is the same slow whole-year fetch sync_tally.py uses)...")
    all_vouchers = st._fetch_all_voucher_records(today, args.dump_raw_dir)
    print(f"  Total vouchers returned: {len(all_vouchers)}")

    delivery_vouchers = [v for v in all_vouchers if st._text(v, "VOUCHERTYPENAME").strip().lower() in delivery_types]
    sales_vouchers = [v for v in all_vouchers if st._text(v, "VOUCHERTYPENAME").strip().lower() in sales_types]
    print(f"  Delivery Note vouchers: {len(delivery_vouchers)}")
    print(f"  Sales vouchers: {len(sales_vouchers)}")

    def tracking_numbers_of(v):
        found = []
        for inv in v.findall(".//ALLINVENTORYENTRIES.LIST"):
            tn = st._text(inv, "TRACKINGNUMBER")
            if tn:
                found.append(tn)
        return found

    with_tn = 0
    without_tn = 0
    sample_shown = 0
    for v in delivery_vouchers:
        tns = tracking_numbers_of(v)
        if tns:
            with_tn += 1
        else:
            without_tn += 1
        if sample_shown < 3:
            print(f"\n--- Sample Delivery Note voucher #{sample_shown + 1} ---")
            print(f"  Voucher No: {st._text(v, 'VOUCHERNUMBER')}  Date: {st._text(v, 'DATE')}  Party: {st._text(v, 'PARTYLEDGERNAME')}")
            print(f"  Tracking numbers found on its inventory entries: {tns or '(none)'}")
            print(f"  Raw XML of this voucher:")
            print("  " + ET.tostring(v, encoding="unicode")[:2000])
            sample_shown += 1

    print(f"\nSUMMARY: {with_tn} of {len(delivery_vouchers)} Delivery Note vouchers have a Tracking Number set; {without_tn} do not.")

    sales_tns = set()
    for v in sales_vouchers:
        sales_tns.update(tracking_numbers_of(v))
    print(f"Distinct tracking numbers seen on Sales vouchers: {len(sales_tns)}")

    if with_tn == 0:
        print("\n==> No Delivery Note in your data has a Tracking Number set.")
        print("    This means Tally has no built-in link between a Delivery Note and any")
        print("    later Sales Invoice for the same goods -- 'billed vs pending' can't be")
        print("    determined automatically from Tracking Numbers. We'd need a different")
        print("    definition of 'pending' (e.g. by party+items+quantity matching, which is")
        print("    much less reliable, or a manual mark-as-billed step in the dashboard).")
    else:
        print("\n==> Tracking Numbers are in use. Send me this full output (redact party names")
        print("    if you'd rather not share them) and I'll wire up real matching logic.")


if __name__ == "__main__":
    main()
