"""
UK Company Financial Distress Monitor — MVP Demo
-------------------------------------------------
Fetches UK companies by SIC code from Companies House API,
checks each for insolvency status, and outputs a CSV report.

Usage:
    python monitor.py --sic 41100
    python monitor.py --sic 56101 --max 50

Requirements:
    pip install requests python-dotenv
"""

import argparse
import csv
import os
import time
from datetime import date
from dotenv import load_dotenv
import requests

load_dotenv()

BASE_URL = "https://api.company-information.service.gov.uk"
OUTPUT_DIR = "output"

START_DATE_TYPES = {
    "petitioned-on", "administration-started-on", "instrumented-on",
    "voluntary-arrangement-started-on", "moratorium-started-on",
}
END_DATE_TYPES = {
    "wound-up-on", "administration-ended-on", "concluded-winding-up-on",
    "case-end-on", "due-to-be-dissolved-on", "administration-discharged-on",
    "declaration-solvent-on",
}


def load_config():
    api_key = os.getenv("CH_API_KEY", "").strip()
    if not api_key:
        raise ValueError("CH_API_KEY not found in .env file.")
    return api_key


def get_companies_by_sic(sic_code, api_key, max_results=100):
    """Fetch companies matching a SIC code via the advanced search endpoint."""
    companies = []
    start_index = 0
    page_size = 100
    total = None

    print(f"Searching Companies House for SIC code: {sic_code}...")

    while True:
        url = f"{BASE_URL}/advanced-search/companies"
        params = {
            "sic_codes": sic_code,
            "items_per_page": page_size,
            "start_index": start_index,
        }
        resp = requests.get(url, auth=(api_key, ""), params=params)

        if resp.status_code == 401:
            raise ValueError("Invalid API key. Check your CH_API_KEY in .env.")
        if resp.status_code != 200:
            print(f"  Warning: search returned {resp.status_code}, stopping pagination.")
            break

        data = resp.json()

        if total is None:
            total = data.get("hits", 0)
            if total == 0:
                print(f"  No companies found for SIC code {sic_code}.")
                return []
            print(f"  Found {total} companies. Fetching up to {min(total, max_results)}...")

        items = data.get("items", [])
        if not items:
            break
        companies.extend(items)

        start_index += len(items)
        if start_index >= min(total, max_results, 1000):
            break

    return companies[:max_results]


def _extract_case_start_date(case):
    """3-pass date extraction: start-type dates → any date → practitioner appointed_on."""
    case_dates = case.get("dates", [])
    for d in case_dates:
        if d.get("type") in START_DATE_TYPES and d.get("date"):
            return d["date"]
    for d in case_dates:
        if d.get("date"):
            return d["date"]
    practitioners = case.get("practitioners", [])
    active = [p["appointed_on"] for p in practitioners if p.get("appointed_on") and not p.get("ceased_to_act_on")]
    return min(active) if active else ""


def _is_case_closed(case):
    """Returns True if case appears concluded (only end dates + all practitioners ceased)."""
    case_dates = case.get("dates", [])
    has_end = any(d.get("type") in END_DATE_TYPES for d in case_dates if d.get("type"))
    has_start = any(d.get("type") in START_DATE_TYPES for d in case_dates if d.get("type"))
    practitioners = case.get("practitioners", [])
    all_ceased = practitioners and all(p.get("ceased_to_act_on") for p in practitioners)
    return has_end and not has_start and all_ceased


def get_insolvency_status(company_number, api_key, months=24):
    """
    Returns insolvency type string for cases opened within `months`, or 'None'.
    Uses 3-pass date extraction. Undatable or old cases are excluded.
    """
    from datetime import datetime, timedelta
    url = f"{BASE_URL}/company/{company_number}/insolvency"
    resp = requests.get(url, auth=(api_key, ""))

    if resp.status_code == 404:
        return "None"
    if resp.status_code == 429:
        print("  Rate limit hit — waiting 10 seconds...")
        time.sleep(10)
        return get_insolvency_status(company_number, api_key, months)
    if resp.status_code != 200:
        return f"Unknown ({resp.status_code})"

    data = resp.json()
    cases = data.get("cases", [])
    if not cases:
        return "None"

    cutoff = datetime.today() - timedelta(days=months * 30)
    recent_types = []

    for case in cases:
        if _is_case_closed(case):
            continue
        start_date_str = _extract_case_start_date(case)
        if not start_date_str:
            continue
        try:
            if datetime.strptime(start_date_str, "%Y-%m-%d") < cutoff:
                continue
        except ValueError:
            continue
        case_type = case.get("type", "unknown")
        if case_type not in recent_types:
            recent_types.append(case_type)

    return ", ".join(recent_types) if recent_types else "None"


def get_last_filing_date(company):
    """Extract last filing date from company profile data (no extra API call)."""
    accounts = company.get("accounts", {})
    last_accounts = accounts.get("last_accounts", {})
    made_up_to = last_accounts.get("made_up_to", "")
    return made_up_to


def write_csv(records, sic_code):
    """Write results to a timestamped CSV in the output directory."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filename = f"{OUTPUT_DIR}/distress_monitor_{sic_code}_{date.today()}.csv"

    fieldnames = [
        "Company Name",
        "Company Number",
        "SIC Code",
        "Company Status",
        "Insolvency Status",
        "Last Filing Date",
    ]

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)

    return filename


def main():
    parser = argparse.ArgumentParser(description="UK Insolvency Monitor — Companies House MVP")
    parser.add_argument("--sic", required=True, help="SIC code to monitor (e.g. 41100)")
    parser.add_argument("--max", type=int, default=100, help="Max companies to check (default: 100)")
    args = parser.parse_args()

    api_key = load_config()
    companies = get_companies_by_sic(args.sic, api_key, max_results=args.max)

    if not companies:
        return

    records = []
    print(f"Checking insolvency status for {len(companies)} companies...")

    for i, company in enumerate(companies, 1):
        name = company.get("company_name", "N/A")
        number = company.get("company_number", "N/A")
        status = company.get("company_status", "N/A")
        sic_codes = company.get("sic_codes", [args.sic])
        last_filing = get_last_filing_date(company)

        insolvency = get_insolvency_status(number, api_key)

        records.append({
            "Company Name": name,
            "Company Number": number,
            "SIC Code": sic_codes[0] if sic_codes else args.sic,
            "Company Status": status,
            "Insolvency Status": insolvency,
            "Last Filing Date": last_filing,
        })

        print(f"  [{i}/{len(companies)}] {name} — {insolvency}")
        time.sleep(0.1)  # Stay within rate limits (600 req/5min)

    output_file = write_csv(records, args.sic)
    print(f"\nDone. {len(records)} companies written to {output_file}")

    # Summary
    distressed = [r for r in records if r["Insolvency Status"] != "None"]
    print(f"Insolvency events detected: {len(distressed)}/{len(records)}")
    if distressed:
        print("\nCompanies with insolvency events:")
        for r in distressed:
            print(f"  - {r['Company Name']} ({r['Company Number']}): {r['Insolvency Status']}")


if __name__ == "__main__":
    main()
