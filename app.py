"""
UK Company Financial Distress Monitor — Streamlit Demo
-------------------------------------------------------
Run with: streamlit run app.py
"""

import os
import time
import csv
import io
from datetime import date
from dotenv import load_dotenv
import requests
import streamlit as st

load_dotenv()

BASE_URL = "https://api.company-information.service.gov.uk"


def load_api_key():
    # Use Streamlit secrets in deployed environment, fall back to .env locally
    try:
        key = st.secrets["CH_API_KEY"].strip()
    except (KeyError, FileNotFoundError):
        key = os.getenv("CH_API_KEY", "").strip()
    if not key:
        st.error("CH_API_KEY not configured. Add it to Streamlit secrets or .env.")
        st.stop()
    return key


def get_companies_by_sic(sic_code, api_key, max_results, progress_text):
    companies = []
    start_index = 0
    page_size = 100
    total = None

    while True:
        url = f"{BASE_URL}/advanced-search/companies"
        params = {
            "sic_codes": sic_code,
            "items_per_page": page_size,
            "start_index": start_index,
        }
        resp = requests.get(url, auth=(api_key, ""), params=params)

        if resp.status_code == 401:
            st.error("Invalid API key. Check your CH_API_KEY in .env.")
            st.stop()
        if resp.status_code != 200:
            break

        data = resp.json()

        if total is None:
            total = data.get("hits", 0)
            if total == 0:
                return []
            progress_text.info(f"Found **{total:,}** registered companies for SIC {sic_code}. Checking first {min(total, max_results)}...")

        items = data.get("items", [])
        companies.extend(items)
        start_index += page_size

        if start_index >= min(total, max_results, 1000):
            break

    return companies[:max_results]


def get_insolvency_status(company_number, api_key):
    url = f"{BASE_URL}/company/{company_number}/insolvency"
    resp = requests.get(url, auth=(api_key, ""))

    if resp.status_code == 404:
        return "None"
    if resp.status_code == 429:
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
    accounts = company.get("accounts", {})
    last_accounts = accounts.get("last_accounts", {})
    return last_accounts.get("made_up_to", "—")


def to_csv(records):
    output = io.StringIO()
    fieldnames = ["Company Name", "Company Number", "SIC Code", "Company Status", "Insolvency Status", "Last Filing Date"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(records)
    return output.getvalue()


# ── Page config ──────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="UK Insolvency Monitor",
    page_icon="🏢",
    layout="centered"
)

st.title("🏢 UK Company Insolvency Monitor")
st.caption("Powered by Companies House API · MVP Demo")

st.markdown("---")

# ── Inputs ───────────────────────────────────────────────────────────────────

col1, col2 = st.columns([2, 1])

with col1:
    sic_code = st.text_input(
        "SIC Code",
        value="41100",
        help="E.g. 41100 = Property Development, 56101 = Restaurants"
    )

with col2:
    max_results = st.slider("Companies to check", min_value=10, max_value=200, value=50, step=10)

run = st.button("▶ Run Monitor", type="primary", use_container_width=True)

# ── Run ───────────────────────────────────────────────────────────────────────

if run:
    if not sic_code.strip():
        st.warning("Please enter a SIC code.")
        st.stop()

    api_key = load_api_key()

    progress_text = st.empty()
    progress_bar = st.progress(0)

    progress_text.info(f"Searching Companies House for SIC code **{sic_code}**...")
    companies = get_companies_by_sic(sic_code, api_key, max_results, progress_text)

    if not companies:
        st.warning(f"No companies found for SIC code {sic_code}.")
        st.stop()

    records = []
    status_container = st.empty()

    for i, company in enumerate(companies):
        name = company.get("company_name", "N/A")
        number = company.get("company_number", "N/A")
        status = company.get("company_status", "N/A")
        sic_codes = company.get("sic_codes", [sic_code])
        last_filing = get_last_filing_date(company)
        insolvency = get_insolvency_status(number, api_key)

        records.append({
            "Company Name": name,
            "Company Number": number,
            "SIC Code": sic_codes[0] if sic_codes else sic_code,
            "Company Status": status,
            "Insolvency Status": insolvency,
            "Last Filing Date": last_filing,
        })

        progress = (i + 1) / len(companies)
        progress_bar.progress(progress)
        status_container.caption(f"Checking {i+1}/{len(companies)}: {name}")
        time.sleep(0.1)

    progress_bar.empty()
    status_container.empty()
    progress_text.empty()

    # ── Results ───────────────────────────────────────────────────────────────

    distressed = [r for r in records if r["Insolvency Status"] != "None"]
    clean = len(records) - len(distressed)

    st.markdown("---")
    st.subheader("Results")

    m1, m2, m3 = st.columns(3)
    m1.metric("Companies Checked", len(records))
    m2.metric("Insolvency Events", len(distressed), delta=None)
    m3.metric("Clean", clean)

    # Colour-code insolvency status
    def highlight_insolvency(row):
        if row["Insolvency Status"] != "None":
            return ["background-color: #fff3cd"] * len(row)
        return [""] * len(row)

    import pandas as pd
    df = pd.DataFrame(records)
    st.dataframe(
        df.style.apply(highlight_insolvency, axis=1),
        use_container_width=True,
        hide_index=True
    )

    # Download
    csv_data = to_csv(records)
    filename = f"distress_monitor_{sic_code}_{date.today()}.csv"
    st.download_button(
        label="⬇ Download CSV",
        data=csv_data,
        file_name=filename,
        mime="text/csv",
        use_container_width=True
    )

    if distressed:
        st.markdown("---")
        st.subheader("⚠ Insolvency Events Detected")
        for r in distressed:
            st.warning(f"**{r['Company Name']}** ({r['Company Number']}) — {r['Insolvency Status']}")
