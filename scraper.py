"""Pulls data from texaslottery.com.

Two sources:
  1. scratchoff.csv  - every prize tier, printed vs claimed, refreshed daily by TLC.
  2. details pages   - total tickets printed and overall odds. Static per game,
                       so we only fetch these once per game number and cache them.
"""

import csv
import io
import re
import time
from datetime import date

import requests
from bs4 import BeautifulSoup

BASE = "https://www.texaslottery.com/export/sites/lottery/Games/Scratch_Offs"
CSV_URL = f"{BASE}/scratchoff.csv"
INDEX_URL = f"{BASE}/all.html"

HEADERS = {
    "User-Agent": "tx-scratch-tracker/1.0 (personal hobby project)",
    "Accept": "text/html,text/csv,*/*",
}

TIMEOUT = 30


def _get(url, retries=3, backoff=2.0):
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    raise last


def _to_int(value):
    value = (value or "").strip().replace(",", "").replace("$", "")
    if value in ("", "-", "--", "---"):
        return 0
    return int(float(value))


def fetch_prize_csv():
    """Return (as_of_date_iso, rows) where rows are
    (game_number, prize_level, total_prizes, prizes_claimed).
    The TOTAL line for each game is stored as prize_level -1.
    """
    text = _get(CSV_URL).text
    lines = text.splitlines()

    as_of = None
    header_idx = 0
    for i, line in enumerate(lines[:5]):
        m = re.search(r"as of\s+(\d{2})/(\d{2})/(\d{4})", line)
        if m:
            as_of = f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
        if line.lstrip('"').startswith("Game Number"):
            header_idx = i
            break

    if as_of is None:
        as_of = date.today().isoformat()

    reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
    rows, meta = [], {}
    for rec in reader:
        try:
            gnum = int(rec["Game Number"])
        except (TypeError, ValueError, KeyError):
            continue

        level_raw = (rec.get("Prize Level") or "").strip()
        printed = _to_int(rec.get("Total Prizes in Level"))
        claimed = _to_int(rec.get("Prizes Claimed"))
        level = -1.0 if level_raw.upper() == "TOTAL" else float(level_raw.replace(",", ""))

        rows.append((gnum, level, printed, claimed))

        close = (rec.get("Game Close Date") or "").strip()
        meta.setdefault(gnum, {
            "game_number": gnum,
            "game_name": (rec.get("Game Name") or "").strip(),
            "ticket_price": float(rec.get("Ticket Price") or 0),
            "close_date": close or None,
        })

    return as_of, rows, meta


def fetch_game_index():
    """Scrape the current games list for detail-page URLs, start dates, and
    the asterisk that marks a game as closing soon."""
    soup = BeautifulSoup(_get(INDEX_URL).text, "html.parser")
    games = {}

    for a in soup.find_all("a", href=True):
        if "details.html_" not in a["href"]:
            continue
        m = re.search(r"(\d{3,5})", a.get_text(strip=True))
        if not m:
            continue
        gnum = int(m.group(1))
        if gnum in games:
            continue

        tr = a.find_parent("tr")
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")] if tr else []

        href = a["href"]
        if href.startswith("/"):
            href = "https://www.texaslottery.com" + href

        start_date, price, closing = None, None, 0
        if len(cells) >= 3:
            if re.match(r"\d{2}/\d{2}/\d{2}", cells[1]):
                mm, dd, yy = cells[1].split("/")
                start_date = f"20{yy}-{mm}-{dd}"
            pm = re.search(r"\$?([\d.]+)", cells[2])
            if pm:
                price = float(pm.group(1))
        closing = 1 if any("*" in c for c in cells[:5]) else 0

        games[gnum] = {
            "game_number": gnum,
            "details_url": href,
            "start_date": start_date,
            "ticket_price": price,
            "closing_soon": closing,
            "active": 1,
        }

    return games


TICKETS_RE = re.compile(r"approximately\s+([\d,]+)\s*\*?\s*tickets", re.I)
ODDS_RE = re.compile(r"overall odds[^.]*?1\s+in\s+([\d.]+)", re.I)


def fetch_game_details(url):
    """Return {'total_tickets': int|None, 'overall_odds': float|None}."""
    text = BeautifulSoup(_get(url).text, "html.parser").get_text(" ", strip=True)
    tickets = TICKETS_RE.search(text)
    odds = ODDS_RE.search(text)
    return {
        "total_tickets": int(tickets.group(1).replace(",", "")) if tickets else None,
        "overall_odds": float(odds.group(1)) if odds else None,
    }
