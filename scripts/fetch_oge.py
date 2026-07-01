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
    """
    Parse an OGE Form 278-T (Periodic Transaction Report) PDF.

    Confirmed column layout from pdfplumber extraction (6 cols):
      0: Row # (may contain multiple #s, or None)
      1: Asset description / company name (CAPS)
      2: Transaction type ("sale", "purchase" — sometimes OCR-garbled)
      3: Date ("5/15/2026", sometimes prefixed "Data\n")
      4: Notification flag ("No", "Yes", or None)
      5: Amount range ("$1 001 -$15 000" — spaces instead of commas due to OCR)

    OGE 278-T does NOT include ticker symbols. We store company name as asset
    and set ticker to "N/A" (growth lookup skipped for unknown tickers).
    """
    trades = []

    # OCR year correction: some scanned PDFs read "2026" as "2028"
    try:
        filing_year = datetime.strptime(filing_date, "%Y-%m-%d").year if filing_date else datetime.now().year
    except ValueError:
        filing_year = datetime.now().year

    # Normalise OCR amount: "$1 001 -$15 000" → "$1,001 - $15,000"
    def _clean_amount(s: str) -> str:
        s = re.sub(r"\$\s*([\d ]+)", lambda m: "$" + m.group(1).replace(" ", ","), s)
        s = re.sub(r",\s*-", " -", s)         # strip trailing comma before dash
        s = re.sub(r"\s*[•·–-]+\s*", " - ", s)
        return s.strip()

    def _parse_date(s: str) -> datetime | None:
        m = re.search(r"\b(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})\b", s)
        if not m:
            return None
        raw = m.group(1).replace("-", "/")
        for fmt in ["%m/%d/%Y", "%m/%d/%y"]:
            try:
                d = datetime.strptime(raw, fmt)
                # Correct OCR year errors (e.g. "2026" misread as "2028")
                if d.year > filing_year:
                    d = d.replace(year=filing_year)
                return d
            except ValueError:
                continue
        return None

    def _parse_type(s: str) -> str:
        s = s.lower().replace("\n", " ")
        if re.search(r"pur|buy|purch", s):
            return "Buy"
        if re.search(r"sal|sell", s):
            return "Sell"
        if "exchange" in s or "xch" in s:
            return "Exchange"
        return "Unknown"

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            log(f"    PDF: {len(pdf.pages)} page(s)")
            for page_num, page in enumerate(pdf.pages):
                for table in page.extract_tables():
                    for row in table:
                        if not row or len(row) < 4:
                            continue
                        cells = [str(c or "").strip() for c in row]

                        # Column 1 must look like a company name (2+ words, mostly alpha/space)
                        asset_raw = cells[1].replace("\n", " ").strip()
                        if len(asset_raw) < 3:
                            continue
                        # Skip header/label rows
                        if any(kw in asset_raw.lower() for kw in (
                            "description", "filer", "donald", "transaction", "trump", "certif"
                        )):
                            continue
                        # Must be mostly uppercase alpha (company name heuristic)
                        alpha = re.sub(r"[^A-Za-z]", "", asset_raw)
                        if not alpha or sum(c.isupper() for c in alpha) / len(alpha) < 0.6:
                            continue

                        # Column 2: transaction type
                        tx_type = _parse_type(cells[2])
                        if tx_type == "Unknown":
                            # Some pages have type in col 1 when col layout shifts
                            tx_type = _parse_type(cells[1])
                            if tx_type == "Unknown":
                                continue

                        # Column 3: date
                        date_str = cells[3]
                        # Try col 4 as fallback (sometimes cols shift)
                        tx_date = _parse_date(date_str) or (
                            _parse_date(cells[4]) if len(cells) > 4 else None
                        )
                        if not tx_date:
                            continue

                        # Column 5: amount (may span cols or be in col 5)
                        amount_raw = ""
                        for idx in (5, 4, 3):
                            if idx < len(cells) and "$" in cells[idx]:
                                amount_raw = _clean_amount(cells[idx])
                                break

                        trades.append({
                            "member": member,
                            "owner": "Filer",
                            "asset": asset_raw,
                            "ticker": "N/A",   # OGE 278-T uses company names, not tickers
                            "type": tx_type,
                            "date": tx_date.strftime("%Y-%m-%d"),
                            "filing_date": filing_date,
                            "amount_raw": amount_raw,
                            "amount_lower": _amount_lower(amount_raw),
                        })

    except Exception as e:
        log(f"  PDF parse error: {e}")

    log(f"    Parsed {len(trades)} trades")
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
    def _is_datatable_json(response):
        return "json" in response.headers.get("content-type", "").lower()

    name_filter = page.locator("input[placeholder='Filter Name']")
    if name_filter.count() > 0:
        log("  Filling 'Filter Name' with 'Trump'")
        name_filter.first.fill("Trump")
        # Wait for DataTables to fire its AJAX call and capture the response
        try:
            with page.expect_response(_is_datatable_json, timeout=12000) as resp_info:
                name_filter.first.press("Enter")
            resp = resp_info.value
            body = resp.body()
            captured_responses.append({"url": resp.url, "status": resp.status, "body": body, "ct": resp.headers.get("content-type","")})
            log(f"  Captured filter response: {resp.url[-80:]}  {len(body)} bytes")
        except PWTimeout:
            log("  Timeout waiting for DataTables filter response")
    else:
        log("  WARNING: 'Filter Name' input not found — dumping all inputs for diagnosis")
        for inp in page.locator("input, select").all():
            try:
                log(f"    id={inp.get_attribute('id')!r} placeholder={inp.get_attribute('placeholder')!r}")
            except Exception:
                pass

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
            if isinstance(row, dict):
                # OGE DataTables dict format:
                #   type: HTML <a href='...PDF...'>278 Transaction</a>
                #   name: "Trump, Donald J"
                #   agency, title, level, docDate, amended
                type_html = str(row.get("type", ""))
                link_m = re.search(r"href=['\"]([^'\"]+\.pdf)['\"]", type_html, re.IGNORECASE)
                pdf_url = link_m.group(1) if link_m else ""
                filing_type = re.sub(r"<[^>]+>", "", type_html).strip()  # "278 Transaction" or "Annual (2026)"
                member = str(row.get("name", "Trump, Donald J")).strip()
                doc_date = str(row.get("docDate", "")).split("T")[0]  # "2026-07-01"

            elif isinstance(row, list):
                # Fallback list format: [date, title, type, name_html, ...]
                type_html = str(row[2]) if len(row) > 2 else ""
                link_m = re.search(r"href=['\"]([^'\"]+\.pdf)['\"]", type_html, re.IGNORECASE)
                pdf_url = link_m.group(1) if link_m else ""
                filing_type = re.sub(r"<[^>]+>", "", type_html).strip()
                name_html = str(row[3]) if len(row) > 3 else ""
                member = re.sub(r"<[^>]+>", "", name_html).strip() or "Donald Trump"
                doc_date = re.sub(r"<[^>]+>", "", str(row[0])).strip().split("T")[0]
            else:
                continue

            # Only keep 278-T periodic transaction reports (skip annual 278)
            if "transaction" not in filing_type.lower() and "278-t" not in filing_type.lower():
                log(f"  Skipping non-PTR filing: {filing_type!r}")
                continue

            if "trump" in member.lower() and pdf_url:
                filings.append({
                    "member": "Donald Trump",
                    "pdf_url": pdf_url,
                    "filing_date": doc_date,
                    "filing_type": filing_type,
                })
                log(f"  PTR: {member}  {filing_type}  {doc_date}  {pdf_url[-60:]}")

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


def _extract_trades_from_filing(filing: dict) -> list:
    """Download the 278-T PDF directly and parse trade transactions from it."""
    member = filing.get("member", "Donald Trump")
    filing_date = filing.get("filing_date", "")
    pdf_url = filing.get("pdf_url", "")

    if not pdf_url:
        log(f"  Skipping {member} — no PDF URL")
        return []

    log(f"  Downloading PDF: {pdf_url[-70:]}")
    try:
        resp = requests.get(pdf_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log(f"  PDF download failed: {e}")
        return []

    trades = _parse_ptr_pdf(resp.content, member, filing_date)
    log(f"  {member} ({filing_date}): {len(trades)} trades from PDF ({len(resp.content)} bytes)")
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
                trades = _extract_trades_from_filing(stub)
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
