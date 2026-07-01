#!/usr/bin/env python3
"""
OGE EFTS 278-T (Periodic Transaction Report) scraper for Donald Trump.

The OGE Electronic Filing and Tracking System (https://efts.oge.gov/EFTS/) is a
React SPA that makes REST API calls. We use Playwright to:
  1. Navigate to the public disclosure search page
  2. Intercept all XHR/fetch calls to discover the API endpoints
  3. Search for Trump's 278-T filings (periodic transaction reports)
  4. Extract trade data from each filing's detail page

Outputs data/trump_trades.json — rolling 90-day window.
"""

import io
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pdfplumber
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

OGE_BASE = "https://www.oge.gov/web/OGE.nsf"
OGE_SEARCH = f"{OGE_BASE}/Officials%20Individual%20Disclosures%20Search%20Collection?OpenForm"
OUTPUT = Path("data/trump_trades.json")
LOOKBACK_DAYS = 90
MAX_FILINGS = 50

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def log(msg):
    print(f"[{datetime.utcnow():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Amount / type helpers (shared with Senate relay)
# ---------------------------------------------------------------------------

def _amount_lower(s: str) -> int:
    nums = re.findall(r"\d+", s.replace(",", "").replace("\n", " "))
    return int(nums[0]) if nums else 0


def _normalize_type(raw: str) -> str:
    if not raw:
        return "Unknown"
    lower = raw.strip().lower()
    if "partial" in lower:
        return "Sell (Partial)"
    if lower.startswith("p") or lower == "purchase":
        return "Buy"
    if lower.startswith("s") or lower == "sale":
        return "Sell"
    if lower.startswith("e"):
        return "Exchange"
    return raw.strip().title()


# ---------------------------------------------------------------------------
# PDF parsing (fallback for paper 278-T filings)
# ---------------------------------------------------------------------------

def _parse_ptr_pdf(pdf_bytes: bytes, member: str, filing_date: str = "") -> list:
    trades = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables():
                    in_data = False
                    for row in table:
                        if not row or not any(row):
                            continue
                        row_text = " ".join(str(c) for c in row if c)
                        if "Transaction" in row_text and "Amount" in row_text:
                            in_data = True
                            continue
                        if not in_data:
                            continue
                        if any(k in row_text for k in ("CERTIFY", "Filing Status")):
                            continue
                        if len(row) >= 7 and row[3] and str(row[3]).strip() in (
                            "S", "P", "E", "S (Partial)", "Purchase", "Sale", "Exchange"
                        ):
                            date_str = str(row[4] or "").strip()
                            amount_raw = str(row[6] or "").replace("\n", " ").strip()
                            ticker_raw = str(row[2] or "")
                            m = re.search(r"\(([A-Z]{1,5})\)", ticker_raw)
                            ticker = m.group(1) if m else "N/A"
                            try:
                                tx_date = datetime.strptime(date_str, "%m/%d/%Y")
                            except ValueError:
                                continue
                            trades.append({
                                "member": member,
                                "owner": str(row[1] or "").strip() or "Filer",
                                "asset": re.sub(r"\s*\([A-Z0-9\.]+\)\s*\[[A-Z]+\].*", "",
                                                ticker_raw).replace("\n", " ").strip(),
                                "ticker": ticker,
                                "type": _normalize_type(str(row[3])),
                                "date": tx_date.strftime("%Y-%m-%d"),
                                "filing_date": filing_date,
                                "amount_raw": amount_raw,
                                "amount_lower": _amount_lower(amount_raw),
                            })
    except Exception as e:
        log(f"  PDF parse error: {e}")
    return trades


# ---------------------------------------------------------------------------
# HTML table parsing (electronic 278-T filings)
# ---------------------------------------------------------------------------

def _parse_transactions_html(html: str, member: str, filing_date: str) -> list:
    """
    Parse electronic 278-T transaction table from OGE EFTS detail page.
    Column order varies; we detect headers and map dynamically.
    """
    trades = []
    soup = BeautifulSoup(html, "html.parser")

    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if not header_row:
            continue
        headers = [th.get_text(strip=True).lower() for th in header_row.find_all(["th", "td"])]
        log(f"    Table headers: {headers}")

        # Must contain date + type + amount at minimum
        if not any("date" in h for h in headers) or not any("amount" in h for h in headers):
            continue

        # Build column index map
        col = {}
        for i, h in enumerate(headers):
            if "date" in h and "transaction" in h:
                col["date"] = i
            elif "date" in h:
                col.setdefault("date", i)
            elif "ticker" in h or "symbol" in h:
                col["ticker"] = i
            elif "asset" in h or "description" in h:
                col["asset"] = i
            elif "type" in h or "transaction type" in h:
                col["type"] = i
            elif "amount" in h:
                col["amount"] = i
            elif "owner" in h:
                col["owner"] = i

        log(f"    Column map: {col}")

        for row in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if not cells:
                continue

            ticker = cells[col["ticker"]].strip() if "ticker" in col and col["ticker"] < len(cells) else "N/A"
            if not ticker or not re.match(r"^[A-Z]{1,5}$", ticker):
                ticker = "N/A"

            date_str = cells[col["date"]].strip() if "date" in col and col["date"] < len(cells) else ""
            tx_date = None
            for fmt in ["%m/%d/%Y", "%Y-%m-%d", "%B %d, %Y"]:
                try:
                    tx_date = datetime.strptime(date_str, fmt)
                    break
                except ValueError:
                    continue
            if not tx_date:
                continue

            amount_raw = cells[col["amount"]].strip() if "amount" in col and col["amount"] < len(cells) else ""
            asset = cells[col["asset"]].strip() if "asset" in col and col["asset"] < len(cells) else ""
            tx_type = cells[col["type"]].strip() if "type" in col and col["type"] < len(cells) else ""
            owner = cells[col["owner"]].strip() if "owner" in col and col["owner"] < len(cells) else "Filer"

            trades.append({
                "member": member,
                "owner": owner,
                "asset": asset,
                "ticker": ticker,
                "type": _normalize_type(tx_type),
                "date": tx_date.strftime("%Y-%m-%d"),
                "filing_date": filing_date,
                "amount_raw": amount_raw,
                "amount_lower": _amount_lower(amount_raw),
            })

    return trades


# ---------------------------------------------------------------------------
# OGE EFTS Playwright scraper
# ---------------------------------------------------------------------------

def _search_oge(page, from_str: str, to_str: str) -> list:
    """
    Navigate OGE EFTS, search for Trump 278-T filings, return list of filing stubs.
    Intercepts all XHR/fetch responses to discover the API endpoints.
    """
    filings = []
    captured_responses = []

    def handle_response(response):
        ct = response.headers.get("content-type", "")
        url = response.url
        if "json" in ct.lower():
            try:
                body = response.body()
                if body:
                    captured_responses.append({"url": url, "status": response.status, "body": body, "ct": ct})
                    log(f"  XHR: {url[-80:]}  status={response.status}  ct={ct[:40]}")
            except Exception as e:
                log(f"  XHR body read error: {e}")

    page.on("response", handle_response)

    # --- Navigate to OGE public disclosure search ---
    log(f"Navigating to {OGE_SEARCH}")
    try:
        page.goto(OGE_SEARCH, wait_until="networkidle", timeout=30000)
    except PWTimeout:
        log("  networkidle timeout on search page — continuing")
    log(f"  Title: {page.title()!r}  URL: {page.url}")

    # Log all form inputs on the page
    inputs = page.locator("input, select, textarea").all()
    log(f"  Form inputs found: {len(inputs)}")
    for inp in inputs[:30]:
        try:
            log(f"    input: id={inp.get_attribute('id')!r} name={inp.get_attribute('name')!r} "
                f"type={inp.get_attribute('type')!r} placeholder={inp.get_attribute('placeholder')!r}")
        except Exception:
            pass

    # Log buttons
    btns = page.locator("button, input[type='submit']").all()
    log(f"  Buttons: {[b.get_attribute('id') or b.inner_text()[:30] for b in btns[:10]]}")

    # Clear captured responses before filtering
    captured_responses.clear()

    # --- Filter using DataTables column filter inputs ---
    # The OGE table uses DataTables with per-column filter inputs:
    #   Filter Date Added | Filter Title | Filter Type | Filter Name | Filter Agency | Filter Level
    name_filter = page.locator("input[placeholder='Filter Name']")
    if name_filter.count() > 0:
        log("  Filling 'Filter Name' with 'Trump'")
        name_filter.first.fill("Trump")
        name_filter.first.press("Enter")
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass
    else:
        log("  WARNING: 'Filter Name' input not found — dumping all inputs for diagnosis")
        for inp in page.locator("input, select").all():
            try:
                log(f"    id={inp.get_attribute('id')!r} placeholder={inp.get_attribute('placeholder')!r}")
            except Exception:
                pass

    # Also try filtering by type — PTR / 278-T
    type_filter = page.locator("input[placeholder='Filter Type']")
    if type_filter.count() > 0:
        type_filter.first.fill("278-T")
        type_filter.first.press("Enter")
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeout:
            pass
        log("  Filtered type by '278-T'")

    # --- Parse captured XHR responses ---
    log(f"  Total XHR responses captured: {len(captured_responses)}")
    for resp in captured_responses:
        log(f"  Analysing: {resp['url'][-80:]}")
        try:
            data = json.loads(resp["body"])
        except Exception:
            log(f"    Not JSON: {resp['body'][:100]}")
            continue

        log(f"  JSON keys: {list(data.keys()) if isinstance(data, dict) else type(data).__name__}")
        if isinstance(data, dict):
            log(f"  JSON preview: {json.dumps(data)[:500]}")

        # Try to find a list of filings in the response
        rows = None
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            for key in ("data", "results", "filings", "disclosures", "items", "records"):
                if key in data and isinstance(data[key], list):
                    rows = data[key]
                    log(f"  Found rows under key '{key}': {len(rows)}")
                    break

        if rows is None:
            continue

        log(f"  Sample rows: {json.dumps(rows[:2])[:600]}")

        for row in rows:
            # OGE DataTables format: [date_added, title_html, type, name_html, agency, level]
            if isinstance(row, list):
                if len(row) < 4:
                    continue
                # Extract name from HTML in column 3: <a href="/...">Lastname, Firstname</a>
                name_html = str(row[3])
                link_m = re.search(r'href=["\']([^"\']+)["\']', name_html)
                name_text = re.sub(r'<[^>]+>', '', name_html).strip()
                filing_type = str(row[2]).strip()   # e.g. "278-T", "278"
                filing_date = re.sub(r'<[^>]+>', '', str(row[0])).strip()
                detail_url = link_m.group(1) if link_m else ""
                if detail_url and not detail_url.startswith("http"):
                    detail_url = "https://www.oge.gov" + detail_url
                member = name_text or "Donald Trump"
            elif isinstance(row, dict):
                first = row.get("firstName", row.get("first_name", row.get("filerFirstName", "")))
                last = row.get("lastName", row.get("last_name", row.get("filerLastName", "")))
                member = f"{first} {last}".strip() or row.get("filerName", row.get("name", "Unknown"))
                filing_date = row.get("filingDate", row.get("filing_date", row.get("dateReceived", "")))
                filing_type = row.get("reportType", row.get("type", ""))
                detail_url = row.get("url", row.get("detailUrl", row.get("link", "")))
                if detail_url and not detail_url.startswith("http"):
                    detail_url = "https://www.oge.gov" + detail_url
            else:
                continue

            if "trump" in member.lower():
                filings.append({
                    "member": member,
                    "detail_url": detail_url,
                    "filing_date": filing_date,
                    "filing_type": filing_type,
                })
                log(f"  Filing: {member}  type={filing_type}  date={filing_date}  url={detail_url[:60]}")

    # --- Fallback: try to find filing links directly on the rendered page ---
    if not filings:
        log("  No filings from XHR — scanning rendered page for links")
        soup = BeautifulSoup(page.content(), "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/278" in href.lower() or "/ptr" in href.lower() or "/disclosure" in href.lower():
                text = a.get_text(strip=True)
                log(f"  Found link: {text!r} → {href}")
                if not href.startswith("http"):
                    href = OGE_BASE + href
                filings.append({
                    "member": "Donald Trump",
                    "detail_url": href,
                    "filing_date": "",
                })

    log(f"Search complete: {len(filings)} filing(s) found")
    return filings


def _extract_trades_from_filing(page, filing: dict) -> list:
    """Navigate to filing detail page and extract trade transactions."""
    member = filing.get("member", "Donald Trump")
    filing_date = filing.get("filing_date", "")
    detail_url = filing.get("detail_url", "")

    if not detail_url:
        log(f"  Skipping {member} — no detail URL")
        return []

    captured_pdf = {}
    captured_json = {}

    def handle_response(response):
        ct = response.headers.get("content-type", "")
        url = response.url
        if "pdf" in ct.lower():
            log(f"    PDF intercepted: {url[-60:]}")
            captured_pdf["bytes"] = response.body()
        elif "json" in ct.lower() and any(k in url for k in ("transaction", "trade", "asset")):
            log(f"    JSON intercepted: {url[-60:]}")
            captured_json["data"] = response.body()
            captured_json["url"] = url

    page.on("response", handle_response)
    try:
        log(f"  Navigating to filing: {detail_url[-70:]}")
        page.goto(detail_url, wait_until="networkidle", timeout=25000)
        log(f"    Title: {page.title()!r}")
    except PWTimeout:
        log(f"    networkidle timeout — continuing")
    except Exception as e:
        log(f"    Nav error: {e}")
    finally:
        page.remove_listener("response", handle_response)

    # PDF fallback
    if "bytes" in captured_pdf:
        trades = _parse_ptr_pdf(captured_pdf["bytes"], member, filing_date)
        log(f"  {member}: {len(trades)} trades from PDF")
        return trades

    # JSON transaction data
    if "data" in captured_json:
        try:
            data = json.loads(captured_json["data"])
            log(f"  JSON data: {json.dumps(data)[:400]}")
            rows = data if isinstance(data, list) else data.get("transactions", data.get("data", []))
            trades = []
            for row in rows:
                ticker = (row.get("ticker") or row.get("symbol") or "N/A").strip()
                if not re.match(r"^[A-Z]{1,5}$", ticker):
                    ticker = "N/A"
                date_str = row.get("transactionDate", row.get("transaction_date", row.get("date", "")))
                tx_date = None
                for fmt in ["%m/%d/%Y", "%Y-%m-%d"]:
                    try:
                        tx_date = datetime.strptime(date_str, fmt)
                        break
                    except ValueError:
                        continue
                if not tx_date:
                    continue
                amount_raw = row.get("amount", row.get("amountRange", ""))
                trades.append({
                    "member": member,
                    "owner": row.get("owner", "Filer"),
                    "asset": row.get("assetName", row.get("asset", "")),
                    "ticker": ticker,
                    "type": _normalize_type(row.get("transactionType", row.get("type", ""))),
                    "date": tx_date.strftime("%Y-%m-%d"),
                    "filing_date": filing_date,
                    "amount_raw": amount_raw,
                    "amount_lower": _amount_lower(amount_raw),
                })
            log(f"  {member}: {len(trades)} trades from JSON")
            return trades
        except Exception as e:
            log(f"  JSON parse error: {e}")

    # HTML table fallback
    html = page.content()
    log(f"  Attempting HTML table parse ({len(html)} bytes)")
    trades = _parse_transactions_html(html, member, filing_date)
    log(f"  {member}: {len(trades)} trades from HTML table")
    return trades


def fetch_trump_trades(from_date: datetime, to_date: datetime) -> list:
    from_str = from_date.strftime("%m/%d/%Y")
    to_str = to_date.strftime("%m/%d/%Y")
    log(f"Launching Playwright for {from_str} → {to_str}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="en-US",
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()
        try:
            filing_stubs = _search_oge(page, from_str, to_str)
            all_trades = []
            for stub in filing_stubs[:MAX_FILINGS]:
                trades = _extract_trades_from_filing(page, stub)
                all_trades.extend(trades)
        finally:
            browser.close()

    log(f"Total trades extracted: {len(all_trades)}")
    return all_trades


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    now = datetime.utcnow()
    cutoff = now - timedelta(days=LOOKBACK_DAYS)
    log(f"=== OGE Trump trades sync: {cutoff.date()} → {now.date()} ===")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    existing = []
    if OUTPUT.exists():
        try:
            existing = json.loads(OUTPUT.read_text()).get("trades", [])
            log(f"Loaded {len(existing)} cached trades")
        except Exception:
            pass

    new_trades = fetch_trump_trades(cutoff, now)

    def key(t):
        return f"{t['member']}|{t['date']}|{t['ticker']}|{t['type']}"

    merged = {key(t): t for t in existing}
    for t in new_trades:
        merged[key(t)] = t

    cutoff_str = cutoff.strftime("%Y-%m-%d")
    final = sorted(
        [t for t in merged.values() if t.get("date", "") >= cutoff_str],
        key=lambda t: t.get("date", ""),
        reverse=True,
    )

    OUTPUT.write_text(json.dumps({
        "last_updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "trades": final,
    }, indent=2))
    log(f"Wrote {len(final)} trades → {OUTPUT}")
    log("=== Done ===")


if __name__ == "__main__":
    main()
