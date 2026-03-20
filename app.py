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
# Date types that indicate when a case STARTED (use for recency check)
# wound-up-on included: CH semantics = "Commencement of winding up" for voluntary liquidations
START_DATE_TYPES = {
    "petitioned-on", "administration-started-on", "instrumented-on",
    "voluntary-arrangement-started-on", "moratorium-started-on",
    "wound-up-on",
}
# Date types that indicate a case has ENDED (case is closed)
# wound-up-on removed (it's a start event per CH API constants.yml)
END_DATE_TYPES = {
    "administration-ended-on", "concluded-winding-up-on",
    "case-end-on", "due-to-be-dissolved-on", "administration-discharged-on",
    "declaration-solvent-on", "moratorium-ended-on", "voluntary-arrangement-ended-on",
}


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


def _fetch_companies_page(sic_code, api_key, start_index, company_status=None,
                          page_size=100, incorporated_from=None):
    """Fetch a single page of companies from the advanced search endpoint.
    CH advanced search uses 'size' (not 'items_per_page'), supports up to 5000.
    """
    params = {"sic_codes": sic_code, "size": page_size, "start_index": start_index}
    if company_status:
        params["company_status"] = company_status
    if incorporated_from:
        params["incorporated_from"] = incorporated_from
    resp = requests.get(f"{BASE_URL}/advanced-search/companies", auth=(api_key, ""), params=params)
    if resp.status_code == 401:
        st.error("Invalid API key. Check your CH_API_KEY in .env.")
        st.stop()
    if resp.status_code != 200:
        return [], 0
    data = resp.json()
    return data.get("items", []), data.get("hits", 0)


def get_companies_by_sic(sic_code, api_key, max_results, progress_text, offset=0):
    """
    Fetch companies by SIC code using two targeted searches:

    Pass 1 — in-process companies (recently incorporated only):
        The CH API has no sort-by-case-date. API returns oldest-registered companies first.
        Strategy: filter to companies incorporated in last 7 years, then fetch the LAST 200
        results. The tail of this set = most recently incorporated in-process companies =
        most recent case dates (confirmed by live API testing: last 200 had 2025-2026 dates).
        Cost: 2 API calls (count + fetch).

    Pass 2 — active companies: fetch max_results (with offset for paging).
    """
    from datetime import datetime
    from dateutil.relativedelta import relativedelta

    companies = []
    seen = set()
    MAX_PAGES = 10
    IN_PROCESS_CAP = 200
    # Only look at companies incorporated in the last 7 years — cuts noise, keeps recent cases
    incorporated_from = (datetime.today() - relativedelta(years=7)).strftime("%Y-%m-%d")

    # Pass 1a — get total count (1 API call)
    _, total_in_process = _fetch_companies_page(
        sic_code, api_key, 0,
        company_status=",".join(ACTIVE_INSOLVENCY_STATUSES),
        incorporated_from=incorporated_from,
        page_size=1
    )

    # Pass 1b — fetch last 200 (most recently incorporated = most recent case dates)
    start_in_process = max(0, total_in_process - IN_PROCESS_CAP)
    items, _ = _fetch_companies_page(
        sic_code, api_key, start_in_process,
        company_status=",".join(ACTIVE_INSOLVENCY_STATUSES),
        incorporated_from=incorporated_from,
        page_size=IN_PROCESS_CAP
    )
    for c in items:
        num = c.get("company_number", "")
        if num not in seen and c.get("company_status", "") not in EXCLUDE_STATUSES:
            companies.append(c)
            seen.add(num)

    in_process_count = len(companies)

    # Pass 2 — active companies (apply user offset for paging)
    start_index = offset
    pages = 0
    total = None
    active_collected = 0
    while active_collected < max_results and pages < MAX_PAGES:
        items, hits = _fetch_companies_page(sic_code, api_key, start_index,
                                            company_status="active", page_size=100)
        if total is None:
            total = hits
        if not items:
            break
        for c in items:
            num = c.get("company_number", "")
            if num not in seen and c.get("company_status", "") not in EXCLUDE_STATUSES:
                companies.append(c)
                seen.add(num)
                active_collected += 1
        start_index += len(items)
        pages += 1
        if not total or start_index >= total:
            break

    progress_text.info(
        f"Found **{len(companies)}** companies to check for SIC {sic_code} "
        f"({in_process_count} in-process + {active_collected} active)..."
    )
    return companies


def _extract_case_start_date(case):
    """
    4-pass extraction of the best available date for an insolvency case.
    Pass 1: prefer start-type dates (petitioned-on, administration-started-on, etc.)
    Pass 2: any non-end-type date
    Pass 3: earliest active practitioner appointed_on
    Pass 4: any date at all — last resort for CH bulk cases that only have wound-up-on.
            Post-2024 CH API increasingly omits start dates for compulsory liquidations.
    Returns date string "YYYY-MM-DD" or "" if truly undatable.
    """
    case_dates = case.get("dates", [])

    # Pass 1 — start-type dates only
    for d in case_dates:
        if d.get("type") in START_DATE_TYPES and d.get("date"):
            return d["date"]

    # Pass 2 — any date that is NOT an end-type
    for d in case_dates:
        if d.get("date") and d.get("type") not in END_DATE_TYPES:
            return d["date"]

    # Pass 3 — active practitioner appointed_on
    practitioners = case.get("practitioners", [])
    active_appointments = [
        p["appointed_on"] for p in practitioners
        if p.get("appointed_on") and not p.get("ceased_to_act_on")
    ]
    if active_appointments:
        return min(active_appointments)

    # Pass 4 — any date at all (e.g. wound-up-on for recent bulk-pipeline cases)
    all_dates = [d["date"] for d in case_dates if d.get("date")]
    if all_dates:
        return min(all_dates)

    return ""


def _is_case_closed(case):
    """
    Conservative closed-case check.
    Only returns True if ALL practitioners have ceased — clear evidence the case concluded.
    Empty practitioners list is NOT treated as closed: official receiver cases have no
    practitioners recorded but are still active. Let the date window filter handle age.
    """
    practitioners = case.get("practitioners", [])
    if not practitioners:
        return False  # No practitioner data — cannot confirm closure
    return all(p.get("ceased_to_act_on") for p in practitioners)


def get_insolvency_status(company_number, api_key, months=24):
    """
    Returns (insolvency_type, case_date) for cases opened within `months`.
    Uses 3-pass date extraction and deduplicates types.
    Undatable or old cases return ("None", "").
    """
    url = f"{BASE_URL}/company/{company_number}/insolvency"
    resp = requests.get(url, auth=(api_key, ""))

    if resp.status_code == 404:
        return "None", ""
    if resp.status_code == 429:
        # Bounded retry — max 3 attempts with backoff, no infinite recursion
        for attempt in range(3):
            time.sleep(10 * (attempt + 1))
            retry = requests.get(url, auth=(api_key, ""))
            if retry.status_code == 200:
                resp = retry
                break
        else:
            return "Rate limited", ""
    if resp.status_code != 200:
        return f"Unknown ({resp.status_code})", ""

    data = resp.json()
    cases = data.get("cases", [])
    if not cases:
        return "None", ""

    from datetime import datetime
    from dateutil.relativedelta import relativedelta
    cutoff = datetime.today() - relativedelta(months=months)

    recent_types = []
    latest_date = ""

    for case in cases:
        # Skip cases that appear concluded
        if _is_case_closed(case):
            continue

        start_date_str = _extract_case_start_date(case)

        # Undatable — can't confirm recency, exclude
        if not start_date_str:
            continue

        # Parse and check within window
        try:
            case_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
        except ValueError:
            continue

        if case_dt < cutoff:
            continue

        # Recent confirmed case — include it
        if not latest_date or start_date_str > latest_date:
            latest_date = start_date_str

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

col1, col2, col3, col4 = st.columns([2, 1, 1, 1])

with col1:
    sic_code = st.text_input(
        "SIC Code",
        value="41100",
        help="E.g. 41100 = Property Development, 56101 = Restaurants"
    )

with col2:
    max_results = st.slider("Companies to check", min_value=10, max_value=100, value=50, step=10)

with col3:
    months_filter = st.selectbox("Case opened within", [6, 12, 24, 36], index=1, format_func=lambda x: f"{x} months")

with col4:
    start_offset = st.number_input(
        "Start from",
        min_value=0,
        value=0,
        step=50,
        help="Skip the first N companies. Use to page through batches — e.g. 0, 50, 100..."
    )

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
    companies = get_companies_by_sic(sic_code, api_key, max_results, progress_text, offset=int(start_offset))

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

    # Only show companies with a CONFIRMED recent insolvency case
    # Companies with active status but no recent case confirmed are silently excluded
    in_process = [r for r in records if r["Company Status"] in ACTIVE_INSOLVENCY_STATUSES and r["Insolvency Status"] != "None"]
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

    # Colour-code by signal strength — only if there's a recent insolvency signal
    def highlight_insolvency(row):
        if row["Insolvency Status"] == "None":
            return [""] * len(row)
        if row["Company Status"] in ACTIVE_INSOLVENCY_STATUSES:
            return ["background-color: #f8d7da"] * len(row)  # red — in process
        return ["background-color: #fff3cd"] * len(row)  # amber — active company, case filed

    import pandas as pd
    actionable = in_process + active_with_case
    df = pd.DataFrame(actionable) if actionable else pd.DataFrame(columns=["Company Name", "Company Number", "SIC Code", "Company Status", "Insolvency Status", "Case Date", "Last Filing Date"])
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
