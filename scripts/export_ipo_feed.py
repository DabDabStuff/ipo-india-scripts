#!/usr/bin/env python3
"""
export_ipo_feed.py — Generates an up-to-date, richly populated `ipo_feed.json`
for `Mod Tools -> Upload & Merge Live IPO Feed (.json)` inside `ipo-india-hub`.

Uses Python standard library only and reads exclusively public market endpoints:
  - IPOWatch Next.js APIs (active/upcoming listings, GMP, GMP history, subscription, KPIs, financials)
  - NSE Live IPO Current Issue & Recent Public Past Issues JSON APIs
  - NSE EOD Bhavcopy CSV archives (latest market prices & listing-day open/high/close)
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import http.cookiejar
import io
import json
import math
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Official NSE equity & ASBA holidays (2026 & 2027), synced with src/shared/calculators.ts
NSE_HOLIDAY_MAP: Dict[str, str] = {
    "2026-01-15": "Municipal Corporation Election / Makar Sankranti",
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-08-26": "Id-E-Milad",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali Balipratipada",
    "2026-11-24": "Guru Nanak Jayanti",
    "2026-12-25": "Christmas",
    "2027-01-26": "Republic Day",
    "2027-03-10": "Id-Ul-Fitr (Ramzan Id)",
    "2027-03-22": "Holi",
    "2027-03-26": "Good Friday",
    "2027-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2027-04-15": "Shri Ram Navami",
    "2027-04-19": "Shri Mahavir Jayanti",
    "2027-05-17": "Bakri Id",
    "2027-06-16": "Muharram",
    "2027-10-29": "Diwali Laxmi Pujan",
}
NSE_HOLIDAYS = set(NSE_HOLIDAY_MAP.keys())

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def ist_today() -> dt.date:
    utc_now = dt.datetime.now(dt.timezone.utc)
    return (utc_now + dt.timedelta(hours=5, minutes=30)).date()


def ist_now_label() -> str:
    utc_now = dt.datetime.now(dt.timezone.utc)
    ist_dt = utc_now + dt.timedelta(hours=5, minutes=30)
    return ist_dt.strftime("%d-%m-%Y %I:%M %p IST")


def is_nse_business_day(d: dt.date) -> bool:
    if d.weekday() >= 5:
        return False
    return d.isoformat() not in NSE_HOLIDAYS


def add_nse_business_days(start: dt.date, count: int) -> dt.date:
    cur = start
    remaining = count
    while remaining > 0:
        cur += dt.timedelta(days=1)
        if is_nse_business_day(cur):
            remaining -= 1
    return cur


def roll_to_next_trading_day(d: dt.date) -> dt.date:
    cur = d
    while not is_nse_business_day(cur):
        cur += dt.timedelta(days=1)
    return cur


def compute_t_plus_3_schedule(close_iso: str, include_anchor: bool = True) -> Dict[str, Optional[str]]:
    close_dt = dt.date.fromisoformat(close_iso)
    t1 = add_nse_business_days(close_dt, 1)
    t2 = add_nse_business_days(close_dt, 2)
    t3 = add_nse_business_days(close_dt, 3)
    return {
        "allotmentDate": t1.isoformat(),
        "unblockDate": t2.isoformat(),
        "listingDate": t3.isoformat(),
        "anchorUnlock30d": roll_to_next_trading_day(t1 + dt.timedelta(days=30)).isoformat() if include_anchor else None,
        "anchorUnlock90d": roll_to_next_trading_day(t1 + dt.timedelta(days=90)).isoformat() if include_anchor else None,
    }


def slugify_company(name: str) -> str:
    cleaned = re.sub(r"\b(limited|ltd|india|private|pvt)\b", "", name.lower())
    cleaned = re.sub(r"[^a-z0-9]+", "-", cleaned).strip("-")
    return cleaned or "ipo-company"


def norm_match_key(name: str) -> str:
    cleaned = re.sub(
        r"\b(limited|ltd|india|private|pvt|technologies|tech|industries|enterprises|systems)\b",
        "",
        (name or "").lower(),
    )
    return re.sub(r"[^a-z0-9]", "", cleaned)


def to_float(val: Any) -> Optional[float]:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val) if math.isfinite(val) else None
    s = re.sub(r"[^0-9.\-]", "", str(val).replace(",", ""))
    if not s or s in {"-", "."}:
        return None
    try:
        num = float(s)
        return num if math.isfinite(num) else None
    except ValueError:
        return None


def parse_single_date(raw: Any, fallback_year: Optional[int] = None) -> Optional[str]:
    if not raw:
        return None
    s = str(raw).strip()
    if not s or "tba" in s.lower():
        return None

    m_iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m_iso:
        return f"{m_iso.group(1)}-{m_iso.group(2)}-{m_iso.group(3)}"

    m_dmy = re.match(r"^(\d{1,2})\s+([A-Za-z]{3,})\s+(\d{4})", s)
    if m_dmy:
        day = int(m_dmy.group(1))
        mon = MONTH_MAP.get(m_dmy.group(2)[:4].lower()) or MONTH_MAP.get(m_dmy.group(2)[:3].lower())
        yr = int(m_dmy.group(3))
        if mon:
            return f"{yr:04d}-{mon:02d}-{day:02d}"

    m_dm = re.match(r"^(\d{1,2})\s+([A-Za-z]{3,})$", s)
    if m_dm and fallback_year:
        day = int(m_dm.group(1))
        mon = MONTH_MAP.get(m_dm.group(2)[:3].lower())
        if mon:
            return f"{fallback_year:04d}-{mon:02d}-{day:02d}"

    m_nse = re.match(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$", s)
    if m_nse:
        day = int(m_nse.group(1))
        mon = MONTH_MAP.get(m_nse.group(2).lower())
        yr = int(m_nse.group(3))
        if mon:
            return f"{yr:04d}-{mon:02d}-{day:02d}"
    return None


def parse_price_band(raw_band: Any, issue_price: Any = None) -> Tuple[float, float]:
    s = str(raw_band or "").strip()
    if s and "tba" not in s.lower():
        nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", s.replace(",", "")) if float(x) > 0]
        if len(nums) >= 2:
            return min(nums[0], nums[-1]), max(nums[0], nums[-1])
        if len(nums) == 1:
            return nums[0], nums[0]
    fallback = to_float(issue_price)
    if fallback and fallback > 0:
        return fallback, fallback
    return 0.0, 0.0


def fetch_json(url: str, opener: Optional[urllib.request.OpenerDirector] = None, timeout: int = 15) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    if opener is not None:
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def fetch_text(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/csv,text/plain,*/*"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def build_nse_opener() -> urllib.request.OpenerDirector:
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [
        ("User-Agent", USER_AGENT),
        ("Accept-Language", "en-US,en;q=0.9"),
        ("Referer", "https://www.nseindia.com/"),
    ]
    try:
        opener.open("https://www.nseindia.com/market-data/all-upcoming-issues-ipo", timeout=10).read()
    except Exception:
        pass
    return opener


def fetch_bhavcopy_for_date(target_date: dt.date) -> Dict[str, Dict[str, Any]]:
    ddmmyyyy = target_date.strftime("%d%m%Y")
    url = f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"
    try:
        csv_text = fetch_text(url, timeout=15)
    except Exception:
        return {}

    quotes: Dict[str, Dict[str, Any]] = {}
    reader = csv.DictReader(io.StringIO(csv_text))
    for raw_row in reader:
        row = {k.strip(): (v or "").strip() for k, v in raw_row.items() if k}
        sym = row.get("SYMBOL", "").upper()
        series = row.get("SERIES", "").upper()
        if not sym or series not in {"EQ", "BE", "SM", "ST"}:
            continue
        open_p = to_float(row.get("OPEN_PRICE"))
        high_p = to_float(row.get("HIGH_PRICE"))
        close_p = to_float(row.get("CLOSE_PRICE"))
        if close_p and close_p > 0:
            prev = quotes.get(sym)
            # Prefer primary EQ/SM rows over BE/ST if both appear
            if prev and prev["series"] in {"EQ", "SM"} and series not in {"EQ", "SM"}:
                continue
            parsed_d1 = parse_single_date(row.get("DATE1")) or target_date.isoformat()
            quotes[sym] = {
                "series": series,
                "date": parsed_d1,
                "open": open_p or close_p,
                "high": high_p or close_p,
                "close": close_p,
            }
    return quotes


def fetch_latest_bhavcopy(today: dt.date) -> Tuple[Optional[str], Dict[str, Dict[str, Any]]]:
    for offset in range(0, 14):
        d = today - dt.timedelta(days=offset)
        quotes = fetch_bhavcopy_for_date(d)
        if quotes:
            return d.isoformat(), quotes
    return None, {}


def extract_ipowatch_subscriptions(detail: Dict[str, Any]) -> Dict[str, Optional[float]]:
    out: Dict[str, Optional[float]] = {
        "qib": None,
        "niiTotal": None,
        "bhni": None,
        "shni": None,
        "retail": None,
        "shareholder": None,
        "employee": None,
        "overall": None,
    }
    candidates: List[Dict[str, Any]] = []
    for key in ("subscriptions", "subscriptionCategory"):
        val = detail.get(key)
        if isinstance(val, list):
            candidates.extend([x for x in val if isinstance(x, dict)])
        elif isinstance(val, dict):
            candidates.append(val)

    # Sort candidates chronologically by subscriptionDate so the latest day's reading wins
    candidates.sort(key=lambda r: str(r.get("subscriptionDate") or ""))

    for row in candidates:
        cat = str(
            row.get("category")
            or row.get("investorCategory")
            or row.get("name")
            or row.get("categoryName")
            or ""
        ).lower()
        sub_val = (
            to_float(row.get("subscriptionValue"))
            if to_float(row.get("subscriptionValue")) is not None
            else (
                to_float(row.get("times"))
                if to_float(row.get("times")) is not None
                else (
                    to_float(row.get("subscription"))
                    if to_float(row.get("subscription")) is not None
                    else (to_float(row.get("subscriptionTimes")) or to_float(row.get("subTimes")))
                )
            )
        )
        if sub_val is None:
            continue
        if "qib" in cat:
            out["qib"] = sub_val
        elif "bhni" in cat or "bnii" in cat or "above" in cat or "> ?10" in cat or "> ₹10" in cat:
            out["bhni"] = sub_val
        elif "shni" in cat or "snii" in cat or "below" in cat or "< ?10" in cat or "< ₹10" in cat:
            out["shni"] = sub_val
        elif "nii" in cat or "hni" in cat:
            out["niiTotal"] = sub_val
        elif "retail" in cat or "rii" in cat:
            out["retail"] = sub_val
        elif "shareholder" in cat:
            out["shareholder"] = sub_val
        elif "employee" in cat or re.search(r"\bemp\b", cat):
            out["employee"] = sub_val
        elif "total" in cat or "overall" in cat:
            out["overall"] = sub_val

    if out["overall"] is None and isinstance(detail.get("subscription"), (int, float, str)):
        out["overall"] = to_float(detail.get("subscription"))
    return out


def extract_ipowatch_fundamentals(detail: Dict[str, Any]) -> Dict[str, Any]:
    pe_upper: Optional[float] = None
    roce_pct: Optional[float] = None
    roe_pct: Optional[float] = None
    debt_eq: Optional[float] = None
    mkt_cap_cr: Optional[float] = None
    face_val_rs: Optional[float] = to_float(detail.get("faceValue"))
    ebitda_margin_pct: Optional[float] = None
    pat_margin_pct: Optional[float] = None
    rev_cr: Optional[float] = None
    pat_cr: Optional[float] = None

    kpi_container = detail.get("kpiValues") or {}
    kpi_list = kpi_container.get("kpiValues") if isinstance(kpi_container, dict) else kpi_container
    if isinstance(kpi_list, list):
        for k in kpi_list:
            if not isinstance(k, dict):
                continue
            label = str(k.get("kpiType", "")).lower()
            val = to_float(k.get("kpiValue"))
            if val is None:
                continue
            if "p/e" in label or "price/earning" in label:
                pe_upper = val
            elif "roce" in label:
                roce_pct = val
            elif "roe" in label or "ronw" in label or "return on net worth" in label:
                roe_pct = val
            elif "debt/equity" in label or "debt to equity" in label:
                debt_eq = val
            elif "market cap" in label:
                mkt_cap_cr = val
            elif "face value" in label and face_val_rs is None:
                face_val_rs = val
            elif "ebitda margin" in label:
                ebitda_margin_pct = val
            elif "pat margin" in label:
                pat_margin_pct = val

    fin_list = detail.get("financialValue")
    fin_history: List[Dict[str, Any]] = []
    if isinstance(fin_list, list):
        by_period: Dict[str, Dict[str, Any]] = {}
        rev_rows = []
        pat_rows = []
        for f in fin_list:
            if not isinstance(f, dict):
                continue
            p_raw = str(f.get("fp_period_name") or "").strip()
            ft = str(f.get("ft_financial_type") or "").strip().lower()
            val = to_float(f.get("fm_financial_value"))
            if not p_raw or val is None:
                continue
            m_yr = re.match(r"^(\d{4})-(\d{2})", p_raw)
            if m_yr:
                yr_short = int(m_yr.group(1)[2:])
                mon = int(m_yr.group(2))
                period_lbl = f"FY{yr_short:02d}" if mon <= 6 else f"FY{yr_short + 1:02d}"
            else:
                period_lbl = p_raw[:10]
            bucket = by_period.setdefault(
                p_raw,
                {"period": period_lbl, "_sortKey": p_raw},
            )
            if "income" in ft or "revenue" in ft:
                rev_rows.append(f)
                bucket["revenueCr"] = round(val, 2)
            elif "ebitda" in ft and "margin" not in ft:
                bucket["ebitdaCr"] = round(val, 2)
            elif "profit after tax" in ft or ft == "pat":
                pat_rows.append(f)
                bucket["patCr"] = round(val, 2)
            elif "net worth" in ft or "networth" in ft:
                bucket["netWorthCr"] = round(val, 2)
            elif "borrowing" in ft or "total debt" in ft:
                bucket["totalDebtCr"] = round(val, 2)

        if rev_rows:
            rev_rows.sort(
                key=lambda r: (
                    str(r.get("fp_period_name", "")).endswith("-03-31"),
                    str(r.get("fp_period_name", "")),
                ),
                reverse=True,
            )
            rev_cr = to_float(rev_rows[0].get("fm_financial_value"))
        if pat_rows:
            pat_rows.sort(
                key=lambda r: (
                    str(r.get("fp_period_name", "")).endswith("-03-31"),
                    str(r.get("fp_period_name", "")),
                ),
                reverse=True,
            )
            pat_cr = to_float(pat_rows[0].get("fm_financial_value"))

        full_fy_rows = [
            row
            for row in by_period.values()
            if "revenueCr" in row and "patCr" in row
        ]
        full_fy_rows.sort(key=lambda r: str(r["_sortKey"]))
        for row in full_fy_rows[-3:]:
            clean = {k: v for k, v in row.items() if k != "_sortKey"}
            fin_history.append(clean)

    if pat_margin_pct is None and rev_cr and rev_cr > 0 and pat_cr is not None:
        pat_margin_pct = round((pat_cr / rev_cr) * 100, 2)

    return {
        "peUpper": pe_upper,
        "rocePct": roce_pct,
        "roePct": roe_pct,
        "debtToEquity": debt_eq,
        "marketCapCr": mkt_cap_cr,
        "faceValueRs": face_val_rs,
        "ebitdaMarginPct": ebitda_margin_pct,
        "patMarginPct": pat_margin_pct,
        "revenueCr": rev_cr,
        "patCr": pat_cr,
        "financialsHistory": fin_history or None,
    }


def resolve_segment_and_exchange(detail: Dict[str, Any]) -> Tuple[str, str]:
    raw_type = str(detail.get("type", "")).lower()
    exchanges = detail.get("exchanges") or []
    exch_str = ", ".join(str(x) for x in exchanges if x) if isinstance(exchanges, list) else str(exchanges)
    combined = f"{raw_type} {exch_str}".lower()

    if "emerge" in combined or "nse sme" in combined:
        return "NSE_EMERGE", exch_str or "NSE Emerge"
    if "bse sme" in combined or "sme" in raw_type:
        return "BSE_SME", exch_str or "BSE SME"
    return "MAINBOARD", exch_str or "NSE, BSE"


IPOWATCH_API_BASES = (
    "https://ipowatch.in/server/api/ipo",
    "https://ipowatch.in/api/ipo",
)


def fetch_ipowatch_endpoint(rel_path: str) -> Dict[str, Any]:
    last_err: Optional[Exception] = None
    clean_rel = rel_path.lstrip("/")
    for base in IPOWATCH_API_BASES:
        try:
            return fetch_json(f"{base}/{clean_rel}")
        except Exception as exc:
            last_err = exc
    if last_err is not None:
        raise last_err
    return {}


def build_ipowatch_records(today: dt.date) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    listings = fetch_ipowatch_endpoint("GetIpoListing").get("data", [])
    by_status = fetch_ipowatch_endpoint("GetIpoListingByStatus").get("data", [])
    gmp_hist_rows = fetch_ipowatch_endpoint("GetIpoGMPHistory").get("result", [])

    gmp_label_by_id: Dict[int, str] = {}
    for row in gmp_hist_rows:
        if isinstance(row, dict) and isinstance(row.get("id"), int):
            gmp_label_by_id[row["id"]] = str(row.get("gmpUpdatedDate") or "")

    seen_ids: Dict[int, Dict[str, Any]] = {}
    for item in [*listings, *by_status]:
        if isinstance(item, dict) and isinstance(item.get("id"), int):
            seen_ids.setdefault(item["id"], item)

    catalog: List[Dict[str, Any]] = []
    drhps: List[Dict[str, Any]] = []
    today_label = ist_now_label()

    for ipo_id, summary in seen_ids.items():
        try:
            detail_rows = fetch_ipowatch_endpoint(str(ipo_id)).get("data") or []
        except Exception as exc:
            print(f"[warn] Failed IPOWatch detail {ipo_id}: {exc}", file=sys.stderr)
            continue
        if not detail_rows:
            continue

        d = detail_rows[0]
        name = str(d.get("name") or summary.get("ipoName") or "").strip()
        if not name:
            continue

        open_date = parse_single_date(d.get("openDate") or summary.get("openDate"), today.year)
        close_date = parse_single_date(d.get("closeDate") or summary.get("closeDate"), today.year)
        price_floor, price_upper = parse_price_band(d.get("priceBand"), d.get("issuePrice"))
        lot_size = int(to_float(d.get("lotSizeForCal")) or to_float(d.get("minBidQuantity")) or 0)
        issue_size_cr = round(to_float(d.get("issueSize")) or 0.0, 2)
        sector = str(d.get("sector") or "Diversified").strip() or "Diversified"
        slug = slugify_company(name)

        # Route issues without schedule dates to DRHP Watchlist; keep dated TBA-band IPOs visible as Price Band Awaited
        if not open_date or not close_date:
            drhps.append({
                "slug": slug,
                "companyName": name,
                "sector": sector,
                "estimatedSizeCr": issue_size_cr,
                "sebiStage": "RHP_EXPECTED" if issue_size_cr > 0 else "DRHP_FILED",
                "expectedWindow": str(summary.get("issueStatus") or "Upcoming Window"),
                "hypeVotes": 0,
            })
            continue
        price_band_pending = price_upper <= 0

        segment, exchange = resolve_segment_and_exchange(d)
        is_mainboard = segment == "MAINBOARD"
        sched = compute_t_plus_3_schedule(close_date, include_anchor=is_mainboard)
        allotment_date = parse_single_date(d.get("allotmentDate"), today.year) or str(sched["allotmentDate"])
        unblock_date = (
            parse_single_date(d.get("refundDate") or d.get("dematCreditDate"), today.year)
            or str(sched["unblockDate"])
        )
        listing_date = parse_single_date(d.get("listingDate"), today.year) or str(sched["listingDate"])

        if price_band_pending:
            lot_size = max(0, lot_size)
            retail_lot_cost = 0
            shni_min_lots = 14 if is_mainboard else 3
            bhni_min_lots = 67 if is_mainboard else 7
        else:
            if lot_size <= 0:
                lot_size = max(1, round(14500 / price_upper)) if is_mainboard else max(1, round(120000 / price_upper))
            retail_lot_cost = round(price_upper * lot_size)
            sme_retail_min = (
                1
                if retail_lot_cost >= 200_000
                else max(2, math.floor(200_000 / max(1, retail_lot_cost)) + 1)
            )
            shni_min_lots = (
                (math.floor(200_000 / max(1, retail_lot_cost)) + 1)
                if is_mainboard
                else max(3, sme_retail_min + 1)
            )
            bhni_min_lots = max(shni_min_lots + 1, math.floor(1_000_000 / max(1, retail_lot_cost)) + 1)

        fresh_cr = round(to_float(d.get("freshValue")) or 0.0, 2)
        ofs_cr = round(to_float(d.get("ipoOFS")) or 0.0, 2)
        if issue_size_cr <= 0 and (fresh_cr + ofs_cr) > 0:
            issue_size_cr = round(fresh_cr + ofs_cr, 2)
        if fresh_cr <= 0 and ofs_cr <= 0:
            fresh_pct = -1.0
        else:
            fresh_pct = round((fresh_cr / issue_size_cr) * 100, 1) if issue_size_cr > 0 else -1.0

        lead_list: List[str] = []
        raw_lms = d.get("leadManagers")
        if isinstance(raw_lms, list):
            for lm in raw_lms:
                if isinstance(lm, dict) and lm.get("leadManagerName"):
                    lead_list.append(str(lm["leadManagerName"]).strip())

        gmp_rs = to_float(d.get("gmp"))
        if gmp_rs is None:
            gmp_rs = to_float(summary.get("gmp"))
        gmp_pct = round((gmp_rs / price_upper) * 100, 1) if (gmp_rs is not None and price_upper > 0) else None

        gmp_history: List[Dict[str, Any]] = []
        for g in (d.get("gmpHistory") or [])[:12]:
            if not isinstance(g, dict):
                continue
            g_val = to_float(g.get("gmp"))
            g_dt = parse_single_date(g.get("date"))
            if g_val is not None and g_dt:
                gmp_history.append({
                    "date": g_dt,
                    "gmpRs": round(g_val, 2),
                    "gmpPct": round((g_val / price_upper) * 100, 1) if price_upper > 0 else 0.0,
                })

        subs = extract_ipowatch_subscriptions(d)
        fundamentals = extract_ipowatch_fundamentals(d)
        ipowatch_list_px = to_float(d.get("listingPrice")) or to_float(summary.get("listingPrice"))

        emp_disc = to_float(d.get("employeeDiscount"))
        has_sh_quota = subs["shareholder"] is not None
        has_emp_quota = (subs["employee"] is not None) or (emp_disc is not None and emp_disc > 0)

        record: Dict[str, Any] = {
            "slug": slug,
            "companyName": name,
            "symbol": str(d.get("symbol") or "").strip().upper() or None,
            "segment": segment,
            "exchange": exchange,
            "sector": sector,
            "priceBandPending": True if price_band_pending else None,
            "registrarName": str(d.get("registrar") or "").strip() or None,
            "leadManager": ", ".join(lead_list) if lead_list else None,
            "leadManagers": lead_list or None,
            "openDate": open_date,
            "closeDate": close_date,
            "allotmentDate": allotment_date,
            "unblockDate": unblock_date,
            "listingDate": listing_date,
            "anchorUnlock30d": sched["anchorUnlock30d"],
            "anchorUnlock90d": sched["anchorUnlock90d"],
            "priceFloor": price_floor,
            "priceUpper": price_upper,
            "lotSize": lot_size,
            "retailLotCost": retail_lot_cost,
            "shniMinLots": shni_min_lots,
            "bhniMinLots": bhni_min_lots,
            "employeeDiscountRs": emp_disc if emp_disc and emp_disc > 0 else None,
            "totalIssueSizeCr": issue_size_cr,
            "freshIssueCr": fresh_cr,
            "ofsIssueCr": ofs_cr,
            "freshIssuePct": fresh_pct,
            "faceValueRs": fundamentals["faceValueRs"],
            "marketCapCr": fundamentals["marketCapCr"],
            "peUpper": fundamentals["peUpper"],
            "revenueCr": fundamentals["revenueCr"],
            "patCr": fundamentals["patCr"],
            "rocePct": fundamentals["rocePct"],
            "roePct": fundamentals["roePct"],
            "ebitdaMarginPct": fundamentals["ebitdaMarginPct"],
            "patMarginPct": fundamentals["patMarginPct"],
            "debtToEquity": fundamentals["debtToEquity"],
            "financialsHistory": fundamentals["financialsHistory"],
            "hasAnchorBook": False if not is_mainboard else None,
            "hasShareholderQuota": True if has_sh_quota else None,
            "hasEmployeeQuota": True if has_emp_quota else None,
            "allotmentOut": listing_date <= today.isoformat() or allotment_date < today.isoformat(),
            "qib": {"sharesSub": subs["qib"], "appsSub": None},
            "niiTotal": {"sharesSub": subs["niiTotal"], "appsSub": None},
            "bhni": {"sharesSub": subs["bhni"], "appsSub": None},
            "shni": {"sharesSub": subs["shni"], "appsSub": None},
            "retail": {"sharesSub": subs["retail"], "appsSub": None},
            "shareholder": {"sharesSub": subs["shareholder"], "appsSub": None} if subs["shareholder"] is not None else None,
            "employee": {"sharesSub": subs["employee"], "appsSub": None} if subs["employee"] is not None else None,
            "overallSharesSub": subs["overall"],
            "overallAppsSub": None,
            "gmpRs": round(gmp_rs, 2) if gmp_rs is not None else None,
            "gmpPct": gmp_pct,
            "gmpUpdatedLabel": gmp_label_by_id.get(ipo_id) or today_label,
            "gmpHistory": gmp_history or None,
            "listingPrice": ipowatch_list_px if (ipowatch_list_px and listing_date <= today.isoformat()) else None,
            "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        # Strip explicit None keys so merge logic only updates non-empty fields cleanly
        catalog.append({k: v for k, v in record.items() if v is not None})

    return catalog, drhps


def fetch_live_nse_holidays(opener: Optional[urllib.request.OpenerDirector] = None) -> Dict[str, str]:
    merged: Dict[str, str] = dict(NSE_HOLIDAY_MAP)
    try:
        op = opener or build_nse_opener()
        data = fetch_json("https://www.nseindia.com/api/holiday-master?type=trading", opener=op, timeout=15)
        rows = []
        if isinstance(data, dict):
            rows = data.get("CM") or data.get("FO") or data.get("CBM") or []
        elif isinstance(data, list):
            rows = data
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                d_iso = parse_single_date(row.get("tradingDate"))
                desc = str(row.get("description") or "").strip()
                if d_iso and desc:
                    merged[d_iso] = desc
                    NSE_HOLIDAYS.add(d_iso)
    except Exception as exc:
        print(f"[warn] NSE holiday-master fetch skipped ({exc}); using built-in NSE holidays.", file=sys.stderr)
    return dict(sorted(merged.items()))


def enrich_with_nse_data(
    catalog: List[Dict[str, Any]],
    today: dt.date,
    prefetched_holidays: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, Dict[str, Any]], set[str], Dict[str, str]]:
    opener = build_nse_opener()
    nse_holidays = prefetched_holidays if prefetched_holidays is not None else fetch_live_nse_holidays(opener=opener)
    past_from = (today - dt.timedelta(days=28)).strftime("%d-%m-%Y")
    past_to = today.strftime("%d-%m-%Y")

    nse_symbol_by_norm: Dict[str, str] = {}
    nse_listed_dates_by_sym: Dict[str, str] = {}

    try:
        past_issues = fetch_json(
            f"https://www.nseindia.com/api/public-past-issues?fromDate={past_from}&toDate={past_to}",
            opener=opener,
        )
        if isinstance(past_issues, list):
            for row in past_issues[:60]:
                if not isinstance(row, dict):
                    continue
                comp = str(row.get("companyName") or row.get("company") or "").strip()
                sym = str(row.get("symbol") or "").strip().upper()
                ld = parse_single_date(row.get("listingDate"))
                if comp and sym:
                    nse_symbol_by_norm[norm_match_key(comp)] = sym
                if sym and ld and ld >= (today - dt.timedelta(days=28)).isoformat():
                    nse_listed_dates_by_sym[sym] = ld
    except Exception as exc:
        print(f"[warn] NSE public-past-issues skipped: {exc}", file=sys.stderr)

    # Also inspect live NSE current issue endpoint for active bidding weekdays
    try:
        cur_issues = fetch_json("https://www.nseindia.com/api/ipo-current-issue", opener=opener)
        if isinstance(cur_issues, list):
            for row in cur_issues:
                if not isinstance(row, dict):
                    continue
                comp = str(row.get("companyName") or "").strip()
                sym = str(row.get("symbol") or "").strip().upper()
                if comp and sym:
                    nse_symbol_by_norm[norm_match_key(comp)] = sym
    except Exception as exc:
        print(f"[warn] NSE ipo-current-issue skipped: {exc}", file=sys.stderr)

    bhav_date, latest_quotes = fetch_latest_bhavcopy(today)
    print(
        f"[info] NSE Bhavcopy latest date={bhav_date or 'none'} symbols={len(latest_quotes)}",
        file=sys.stderr,
    )

    listing_day_cache: Dict[str, Dict[str, Dict[str, Any]]] = {}
    daily_cache_30d: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for offset in range(0, 31):
        d = today - dt.timedelta(days=offset)
        if is_nse_business_day(d):
            q_day = fetch_bhavcopy_for_date(d)
            if q_day:
                daily_cache_30d[d.isoformat()] = q_day

    for rec in catalog:
        nk = norm_match_key(rec["companyName"])
        sym = rec.get("symbol") or nse_symbol_by_norm.get(nk)
        if sym:
            rec["symbol"] = sym
            q = latest_quotes.get(sym)
            if q:
                rec["currentMarketPrice"] = q["close"]
            l_date = rec.get("listingDate") or nse_listed_dates_by_sym.get(sym)
            if l_date and l_date <= today.isoformat() and rec.get("priceUpper", 0) > 0:
                if l_date not in listing_day_cache:
                    listing_day_cache[l_date] = daily_cache_30d.get(l_date) or fetch_bhavcopy_for_date(dt.date.fromisoformat(l_date))
                lq = listing_day_cache[l_date].get(sym)
                if lq:
                    rec["listingPrice"] = lq["open"]
                    rec["day1HighPrice"] = lq["high"]
                    rec["day1ClosePrice"] = lq["close"]
                    rec["listingDayGainPct"] = round(
                        ((lq["open"] - rec["priceUpper"]) / rec["priceUpper"]) * 100,
                        1,
                    )
                cutoff_30d = (dt.date.fromisoformat(l_date) + dt.timedelta(days=30)).isoformat()
                pts: List[Dict[str, Any]] = []
                for d_iso in sorted(daily_cache_30d.keys()):
                    if l_date <= d_iso <= cutoff_30d and sym in daily_cache_30d[d_iso]:
                        px = daily_cache_30d[d_iso][sym]["close"]
                        pts.append({
                            "date": d_iso,
                            "closePriceRs": round(px, 2),
                            "changeFromIssuePct": round(((px - rec["priceUpper"]) / rec["priceUpper"]) * 100, 2),
                        })
                if pts:
                    rec["eodHistory"] = pts

    matched_syms = set(nse_listed_dates_by_sym.keys()) | set(nse_symbol_by_norm.values())
    return latest_quotes, matched_syms, nse_holidays


def collect_known_seed_symbols() -> set[str]:
    seed_path = Path(__file__).resolve().parent.parent / "src" / "shared" / "seedData.ts"
    if not seed_path.exists():
        return set()
    text = seed_path.read_text(encoding="utf-8", errors="ignore")
    return set(re.findall(r"['\"]?symbol['\"]?\s*:\s*['\"]([A-Z0-9_\-&]+)['\"]", text))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export live Indian IPO feed (IPOWatch + NSE) as Mod Tools merge JSON."
    )
    default_out = Path(__file__).resolve().parent.parent / "ipo_feed.json"
    parser.add_argument(
        "--out",
        type=Path,
        default=default_out,
        help=f"Output JSON file path (default: {default_out})",
    )
    parser.add_argument(
        "--all-eod",
        action="store_true",
        help="Include all ~3,400 NSE Bhavcopy symbols instead of IPO-matched symbols only.",
    )
    args = parser.parse_args()

    today = ist_today()
    print(f"[info] Fetching live IPO feed for {today.isoformat()}...", file=sys.stderr)

    # Fetch live NSE holidays FIRST so T+3 schedule calculation uses 2028+ dynamic holidays cleanly
    nse_opener = build_nse_opener()
    nse_holidays = fetch_live_nse_holidays(opener=nse_opener)

    try:
        catalog, drhps = build_ipowatch_records(today)
    except Exception as exc:
        print(f"[warn] IPOWatch feed fetch skipped ({exc}); continuing with NSE Bhavcopy & Holidays only.", file=sys.stderr)
        catalog, drhps = [], []

    latest_quotes, nse_recent_syms, nse_holidays = enrich_with_nse_data(
        catalog,
        today,
        prefetched_holidays=nse_holidays,
    )

    relevant_syms = (
        collect_known_seed_symbols()
        | nse_recent_syms
        | {str(r["symbol"]).upper() for r in catalog if r.get("symbol")}
    )

    eod_by_symbol = {
        sym: {"close": q["close"], "open": q["open"], "high": q["high"], "date": q["date"]}
        for sym, q in latest_quotes.items()
        if args.all_eod or sym in relevant_syms
    }

    payload = {
        "exportedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": "python_ipowatch_nse_sync",
        "catalog": catalog,
        "drhpWatchlist": drhps,
        "eodQuotesBySymbol": eod_by_symbol,
        "nseHolidays": nse_holidays,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print(
        f"[ok] Wrote {args.out} ({len(catalog)} active/priced IPOs, {len(drhps)} DRHP watchlist items, {len(eod_by_symbol)} NSE Bhavcopy EOD quotes)"
    )
    for item in catalog:
        gmp_str = f"GMP ₹{item['gmpRs']} ({item.get('gmpPct', 0)}%)" if "gmpRs" in item else "GMP N/A"
        print(
            f"  - {item['companyName']} [{item['segment']}] {item['openDate']}..{item['closeDate']} "
            f"| ₹{item['priceFloor']}-₹{item['priceUpper']} | Lot {item['lotSize']} | {gmp_str}"
        )
    for d in drhps:
        print(f"  - [DRHP] {d['companyName']} ({d['sector']}) | {d['expectedWindow']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
