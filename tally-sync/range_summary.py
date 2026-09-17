#!/usr/bin/env python3
"""
Reads every day already synced into Firestore between --from and --to
(inclusive) and totals total_income/total_expense across that range --
for narrowing down a discrepancy by bisecting a month/period in half
instead of checking every single day.

Read-only -- never writes anything, never talks to Tally. Uses the same
service-account.json already sitting next to sync_tally.py.

Usage:
  python range_summary.py --from 2026-04-01 --to 2026-04-15
"""

import argparse
import datetime
import os

import firebase_admin
from firebase_admin import credentials, firestore

SERVICE_ACCOUNT_PATH = os.path.join(os.path.dirname(__file__), "service-account.json")
FIRESTORE_COLLECTION = "daily_reports"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="from_date", required=True, help="YYYY-MM-DD, inclusive")
    parser.add_argument("--to", dest="to_date", required=True, help="YYYY-MM-DD, inclusive")
    args = parser.parse_args()

    if not os.path.exists(SERVICE_ACCOUNT_PATH):
        raise RuntimeError(f"Service account key not found at {SERVICE_ACCOUNT_PATH}.")

    cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
    firebase_admin.initialize_app(cred)
    db = firestore.client()

    total_income = 0.0
    total_expense = 0.0
    days_found = 0
    needs_review_days = []
    missing_days = []

    cur = datetime.datetime.strptime(args.from_date, "%Y-%m-%d").date()
    end = datetime.datetime.strptime(args.to_date, "%Y-%m-%d").date()
    while cur <= end:
        date_iso = cur.strftime("%Y-%m-%d")
        doc = db.collection(FIRESTORE_COLLECTION).document(date_iso).get()
        if not doc.exists:
            missing_days.append(date_iso)
        else:
            data = doc.to_dict()
            pl = data.get("profit_and_loss") or {}
            days_found += 1
            if pl.get("needs_review"):
                needs_review_days.append(date_iso)
            else:
                total_income += pl.get("total_income") or 0.0
                total_expense += pl.get("total_expense") or 0.0
        cur += datetime.timedelta(days=1)

    print(f"Range {args.from_date} to {args.to_date}: {days_found} day(s) found")
    if missing_days:
        print(f"  MISSING (never synced): {missing_days}")
    if needs_review_days:
        print(f"  NEEDS REVIEW (P&L could not be parsed): {needs_review_days}")
    print(f"  Total Income:  {total_income:>16,.2f}")
    print(f"  Total Expense: {total_expense:>16,.2f}")
    print(f"  Flow Net:      {total_income - total_expense:>16,.2f}")


if __name__ == "__main__":
    main()
