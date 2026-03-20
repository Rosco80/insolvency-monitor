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
        companies.extend(items)

        start_index += page_size
        if start_index >= min(total, max_results, 1000):
            break

    return companies[:max_results]


def get_insolvency_status(company_number, api_key):
    """
    Check insolvency status for a single company.
    Returns a string describing case type(s), or 'None' if clean.
    404 = no insolvency record (treat as clean, not an error).
    """
    url = f"{BASE_URL}/company/{company_number}/insolvency"
    resp = requests.get(url, auth=(api_key, ""))

    if resp.status_code == 404:
        return "None"
    if resp.status_code == 429:
        print("  Rate limit hit — waiting 10 seconds...")
        time.sleep(10)
        return get_insolvency_status(company_number, api_key)
    if resp.status_code != 200:
        return f"Unknown ({resp.status_code})"

    data = resp.json()
    cases = data.get("cases", [])
    if not cases:
        return "None"

    types = [c.get("type", "unknown") for c in cases]
    return ", ".join(types)


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
