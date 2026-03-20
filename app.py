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

# Companies already wound up — no monitoring value
EXCLUDE_STATUSES = {"dissolved", "converted-closed", "removed", "closed"}
# Currently in an active insolvency process
ACTIVE_INSOLVENCY_STATUSES = {"liquidation", "administration", "receivership", "voluntary-arrangement", "insolvency-proceedings"}


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
        if not items:
            break
        # Filter out companies already wound up — no monitoring value
        filtered = [c for c in items if c.get("company_status", "") not in EXCLUDE_STATUSES]
        companies.extend(filtered)
        start_index += len(items)

        if start_index >= min(total, max_results, 1000):
            break

    return companies[:max_results]


def get_insolvency_status(company_number, api_key, months=24):
    """
    Returns (insolvency_type, case_date) for cases opened within `months`.
    Deduplicates types and filters out old historical cases.
    """
    url = f"{BASE_URL}/company/{company_number}/insolvency"
    resp = requests.get(url, auth=(api_key, ""))

    if resp.status_code == 404:
        return "None", ""
    if resp.status_code == 429:
        time.sleep(10)
        return get_insolvency_status(company_number, api_key, months)
    if resp.status_code != 200:
        return f"Unknown ({resp.status_code})", ""

    data = resp.json()
    cases = data.get("cases", [])
    if not cases:
        return "None", ""

    from datetime import datetime, timedelta
    cutoff = datetime.today() - timedelta(days=months * 30)

    recent_types = []
    latest_date = ""

    for case in cases:
        # Extract the earliest/most relevant date from the case
        case_dates = case.get("dates", [])
        case_date_str = ""
        for d in case_dates:
            if d.get("date"):
                case_date_str = d["date"]
                break

        # Filter: only include if case date is within the window
        if case_date_str:
            try:
                case_dt = datetime.strptime(case_date_str, "%Y-%m-%d")
                if case_dt < cutoff:
                    continue  # Too old — skip
                if not latest_date or case_date_str > latest_date:
                    latest_date = case_date_str
            except ValueError:
                pass

        case_type = case.get("type", "unknown")
        if case_type not in recent_types:
            recent_types.append(case_type)

    if not recent_types:
        return "None", ""

    return ", ".join(recent_types), latest_date


def get_company_profile(company_number, api_key):
    """Fetch full company profile to get last filing date."""
    url = f"{BASE_URL}/company/{company_number}"
    resp = requests.get(url, auth=(api_key, ""))
    if resp.status_code != 200:
        return {}
    return resp.json()


def get_last_filing_date(profile):
    """Return most recent filing date from accounts or confirmation statement."""
    dates = []
    accounts_date = profile.get("accounts", {}).get("last_accounts", {}).get("made_up_to", "")
    confirmation_date = profile.get("confirmation_statement", {}).get("last_made_up_to", "")
    if accounts_date:
        dates.append(accounts_date)
    if confirmation_date:
        dates.append(confirmation_date)
    return max(dates) if dates else "—"


def to_csv(records):
    output = io.StringIO()
    fieldnames = ["Company Name", "Company Number", "SIC Code", "Company Status", "Insolvency Status", "Case Date", "Last Filing Date"]
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

col1, col2, col3 = st.columns([2, 1, 1])

with col1:
    sic_code = st.text_input(
        "SIC Code",
        value="41100",
        help="E.g. 41100 = Property Development, 56101 = Restaurants"
    )

with col2:
    max_results = st.slider("Companies to check", min_value=10, max_value=200, value=50, step=10)

with col3:
    months_filter = st.selectbox("Case opened within", [6, 12, 24, 36], index=1, format_func=lambda x: f"{x} months")

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
        profile = get_company_profile(number, api_key)
        last_filing = get_last_filing_date(profile)
        insolvency, case_date = get_insolvency_status(number, api_key, months=months_filter)

        records.append({
            "Company Name": name,
            "Company Number": number,
            "SIC Code": sic_codes[0] if sic_codes else sic_code,
            "Company Status": status,
            "Insolvency Status": insolvency,
            "Case Date": case_date,
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

    # Active signals only — dissolved companies already filtered at fetch time
    in_process = [r for r in records if r["Company Status"] in ACTIVE_INSOLVENCY_STATUSES]
    active_with_case = [r for r in records if r["Company Status"] == "active" and r["Insolvency Status"] != "None"]
    clean = [r for r in records if r["Insolvency Status"] == "None" and r["Company Status"] not in ACTIVE_INSOLVENCY_STATUSES]

    st.markdown("---")
    st.subheader("Results")
    st.caption(f"Dissolved and wound-up companies excluded. Showing active monitoring signals only.")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Companies Checked", len(records))
    m2.metric("In Process", len(in_process), help="Currently in liquidation, administration or receivership")
    m3.metric("Early Warning", len(active_with_case), help="Still active but insolvency case filed — act now")
    m4.metric("Clean", len(clean))

    # Colour-code by signal strength
    def highlight_insolvency(row):
        if row["Company Status"] in ACTIVE_INSOLVENCY_STATUSES:
            return ["background-color: #f8d7da"] * len(row)  # red — in process
        if row["Insolvency Status"] != "None":
            return ["background-color: #fff3cd"] * len(row)  # amber — early warning
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

    if active_with_case:
        st.markdown("---")
        st.subheader("🚨 Early Warning — Active Companies with Insolvency Filing")
        for r in active_with_case:
            st.warning(f"**{r['Company Name']}** ({r['Company Number']}) — {r['Insolvency Status']}")

    if in_process:
        st.markdown("---")
        st.subheader("⚠ Currently In Insolvency Process")
        for r in in_process:
            st.error(f"**{r['Company Name']}** ({r['Company Number']}) — {r['Company Status']} / {r['Insolvency Status']}")
