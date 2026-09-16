#!/usr/bin/env python3
"""
Reads every day already synced into Firestore (daily_reports/YYYY-MM-DD)
and totals total_income/total_expense by calendar month, so the month
with a real discrepancy can be found by comparing each month's figures
here against Tally's own monthly P&L, instead of checking single days
one at a time out of a whole financial year.

Reads only -- never writes anything, and never talks to Tally. Uses the
same service-account.json already sitting next to sync_tally.py.

Usage:
  python monthly_summary.py
"""

import collections
import os

import firebase_admin
from firebase_admin import credentials, firestore

SERVICE_ACCOUNT_PATH = os.path.join(os.path.dirname(__file__), "service-account.json")
FIRESTORE_COLLECTION = "daily_reports"


def main():
    if not os.path.exists(SERVICE_ACCOUNT_PATH):
        raise RuntimeError(f"Service account key not found at {SERVICE_ACCOUNT_PATH}.")

    cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
    firebase_admin.initialize_app(cred)
    db = firestore.client()

    by_month = collections.defaultdict(lambda: {"income": 0.0, "expense": 0.0, "days": 0, "needs_review_days": []})

    for doc in db.collection(FIRESTORE_COLLECTION).stream():
        data = doc.to_dict()
        date_iso = data.get("date", doc.id)
        month = date_iso[:7]  # "YYYY-MM"
        pl = data.get("profit_and_loss") or {}
        bucket = by_month[month]
        bucket["days"] += 1
        if pl.get("needs_review"):
            bucket["needs_review_days"].append(date_iso)
            continue
        bucket["income"] += pl.get("total_income") or 0.0
        bucket["expense"] += pl.get("total_expense") or 0.0

    print(f"{'Month':<10} {'Days':>5} {'Total Income':>16} {'Total Expense':>16} {'Flow Net':>16}")
    print("-" * 70)
    running_income = 0.0
    running_expense = 0.0
    for month in sorted(by_month):
        b = by_month[month]
        net = b["income"] - b["expense"]
        running_income += b["income"]
        running_expense += b["expense"]
        flag = f"  <-- {len(b['needs_review_days'])} day(s) needs_review: {b['needs_review_days']}" if b["needs_review_days"] else ""
        print(f"{month:<10} {b['days']:>5} {b['income']:>16,.2f} {b['expense']:>16,.2f} {net:>16,.2f}{flag}")
    print("-" * 70)
    print(f"{'TOTAL':<10} {'':>5} {running_income:>16,.2f} {running_expense:>16,.2f} {running_income - running_expense:>16,.2f}")


if __name__ == "__main__":
    main()
