#!/usr/bin/env python3
"""
Crypto prediction accuracy tracker (simple, educational).
- Stores predictions in SQLite
- Pulls predictions from CoinCodex (API if available; otherwise simple scrape)
- Pulls actual price from CoinGecko history endpoint
- Evaluates errors and stores results
"""

import sqlite3
import requests
import time
import logging
from bs4 import BeautifulSoup
from datetime import datetime, date
from dateutil import parser
import os

# ---------- CONFIG ----------
DB_PATH = os.environ.get("TRACKER_DB", "predictions.db")
USER_AGENT = "crypto-accuracy-tracker/1.0 (+https://example.local/)"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
COINCodex_BASE = "https://coincodex.com"   # site root (API docs: coincodex.com/page/api/)
# Which symbols to track (CoinGecko IDs and a human symbol)
TRACK = [
    {"gecko_id": "bitcoin", "symbol": "BTC"},
    # add others like {"gecko_id": "ethereum", "symbol": "ETH"}
]
# ----------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")


# ---------- DB ----------
def init_db(conn):
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS predictions (
        id INTEGER PRIMARY KEY,
        symbol TEXT NOT NULL,
        source TEXT NOT NULL,
        prediction_date TEXT NOT NULL,
        target_date TEXT NOT NULL,
        predicted_price REAL NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS results (
        id INTEGER PRIMARY KEY,
        prediction_id INTEGER NOT NULL,
        actual_price REAL,
        abs_error REAL,
        pct_error REAL,
        evaluated_on TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(prediction_id) REFERENCES predictions(id)
    )
    """)
    conn.commit()


# ---------- CoinCodex: try API then fallback to scrape ----------
def fetch_predictions_from_coincodex(symbol):
    """
    Attempt to fetch predictions for `symbol` from CoinCodex.
    This function is intentionally simple and defensive:
    1) Try an undocumented API path (commonly used by community) - if it works.
    2) Fallback: scrape the coin page and attempt to parse visible 'price prediction' area.

    Returns a list of dicts: [{"target_date": "YYYY-MM-DD", "predicted_price": 12345.67, "source":"CoinCodex"}, ...]
    """
    headers = {"User-Agent": USER_AGENT}
    results = []

    # Strategy 1: try basic API-ish endpoint (may or may not be available)
    try:
        api_url = f"{COINCodex_BASE}/api/coincodex/get_coin/{symbol.lower()}"
        r = requests.get(api_url, headers=headers, timeout=15)
        if r.status_code == 200:
            j = r.json()
            # best-effort extraction; real structure may differ - adapt as needed
            if isinstance(j, dict) and "price_prediction" in j:
                preds = j.get("price_prediction") or []
                for p in preds:
                    # expect p to carry a date and price; guard carefully
                    dt = p.get("date") or p.get("target_date") or None
                    price = p.get("price") or p.get("predicted_price") or None
                    if dt and price:
                        try:
                            parsed = parser.parse(dt).date()
                        except Exception:
                            # try dd-mm-yyyy formats, etc.
                            parsed = parser.parse(dt, dayfirst=False).date()
                        results.append({
                            "target_date": parsed.isoformat(),
                            "predicted_price": float(price),
                            "source": "CoinCodex"
                        })
        else:
            logging.debug("CoinCodex API attempt returned status %s", r.status_code)
    except Exception as e:
        logging.debug("CoinCodex API attempt failed: %s", e)

    # Strategy 2: fallback scrape
    if not results:
        try:
            page_url = f"{COINCodex_BASE}/currency/{symbol.lower()}"  # e.g. /currency/bitcoin
            r = requests.get(page_url, headers=headers, timeout=15)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                # This is heuristic: look for sections titled 'Price Prediction' or similar.
                # You will need to adapt selectors if the site structure changes.
                candidate = soup.find(lambda tag: tag.name == "h2" and "price prediction" in tag.text.lower())
                if candidate:
                    # find following siblings and parse numbers/dates
                    block = candidate.find_next_sibling()
                    if block:
                        # naive number/date extraction (educational)
                        text = block.get_text(" | ", strip=True)
                        # look for date-like and $-like numbers in the text
                        import re
                        date_matches = re.findall(r"(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})", text)
                        price_matches = re.findall(r"\$?\s?([0-9]{1,3}(?:[,][0-9]{3})*(?:\.\d+)?)", text)
                        if date_matches and price_matches:
                            # pair them in order (best-effort)
                            for i in range(min(len(date_matches), len(price_matches))):
                                dt = date_matches[i]
                                pr = price_matches[i].replace(',', '')
                                try:
                                    parsed = parser.parse(dt, dayfirst=False).date()
                                    results.append({
                                        "target_date": parsed.isoformat(),
                                        "predicted_price": float(pr),
                                        "source": "CoinCodex-scrape"
                                    })
                                except Exception:
                                    continue
            else:
                logging.debug("CoinCodex page returned status %s", r.status_code)
        except Exception as e:
            logging.debug("CoinCodex scrape failed: %s", e)

    return results


# ---------- CoinGecko: get actual price on target date ----------
def get_actual_price_from_coingecko(gecko_id, target_date):
    """
    Returns the USD price for the coin on target_date (YYYY-MM-DD) using CoinGecko history endpoint.
    CoinGecko expects date in DD-MM-YYYY for the /history endpoint.
    """
    headers = {"User-Agent": USER_AGENT}
    dt = datetime.fromisoformat(target_date).date()
    date_for_api = dt.strftime("%d-%m-%Y")
    url = f"{COINGECKO_BASE}/coins/{gecko_id}/history?date={date_for_api}"
    r = requests.get(url, headers=headers, timeout=15)
    if r.status_code == 200:
        j = r.json()
        md = j.get("market_data")
        if md and "current_price" in md and "usd" in md["current_price"]:
            return float(md["current_price"]["usd"])
    logging.warning("CoinGecko history fetch failed for %s on %s (status %s). Response: %s",
                    gecko_id, target_date, r.status_code, r.text[:200])
    return None


# ---------- DB helpers ----------
def insert_prediction(conn, symbol, source, prediction_date, target_date, predicted_price):
    c = conn.cursor()
    c.execute("""
    INSERT INTO predictions (symbol, source, prediction_date, target_date, predicted_price)
    VALUES (?, ?, ?, ?, ?)
    """, (symbol, source, prediction_date, target_date, predicted_price))
    conn.commit()
    return c.lastrowid


def find_due_predictions(conn, as_of_date=None):
    """
    Find predictions with target_date <= as_of_date that don't have results yet.
    """
    if as_of_date is None:
        as_of_date = date.today().isoformat()
    c = conn.cursor()
    c.execute("""
    SELECT p.id, p.symbol, p.source, p.target_date, p.predicted_price
    FROM predictions p
    LEFT JOIN results r ON r.prediction_id = p.id
    WHERE r.id IS NULL AND DATE(p.target_date) <= DATE(?)
    """, (as_of_date,))
    return c.fetchall()


def insert_result(conn, prediction_id, actual_price, abs_error, pct_error):
    c = conn.cursor()
    c.execute("""
    INSERT INTO results (prediction_id, actual_price, abs_error, pct_error)
    VALUES (?, ?, ?, ?)
    """, (prediction_id, actual_price, abs_error, pct_error))
    conn.commit()
    return c.lastrowid


# ---------- MAIN RUN ----------
def main():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    today_iso = date.today().isoformat()
    logging.info("Tracker run for %s", today_iso)

    # 1) Fetch predictions (for each tracked coin)
    for coin in TRACK:
        symbol = coin["symbol"]
        gecko_id = coin["gecko_id"]

        logging.info("Fetching predictions for %s", symbol)
        preds = fetch_predictions_from_coincodex(gecko_id)
        if not preds:
            logging.info("No predictions found for %s this run.", symbol)
        else:
            for p in preds:
                # Store prediction with prediction_date == today
                pid = insert_prediction(conn, symbol, p.get("source", "CoinCodex"), today_iso, p["target_date"], p["predicted_price"])
                logging.info("Stored prediction %s -> %s (id=%s)", symbol, p["target_date"], pid)
                # be gentle with target site
                time.sleep(1)

    # 2) Evaluate due predictions (target_date <= today) that aren't evaluated yet
    due = find_due_predictions(conn, today_iso)
    if not due:
        logging.info("No due predictions to evaluate today.")
    else:
        logging.info("Found %d due predictions to evaluate", len(due))
        for row in due:
            prediction_id, symbol, source, target_date, predicted_price = row
            # find gecko_id mapping
            gecko_id = next((c["gecko_id"] for c in TRACK if c["symbol"] == symbol), None)
            if not gecko_id:
                logging.error("No CoinGecko id mapping for symbol %s; skipping", symbol)
                continue
            actual = get_actual_price_from_coingecko(gecko_id, target_date)
            if actual is None:
                logging.warning("Could not fetch actual price for %s on %s", symbol, target_date)
                continue
            abs_error = abs(predicted_price - actual)
            pct_error = (abs_error / actual) * 100 if actual != 0 else None
            insert_result(conn, prediction_id, actual, abs_error, pct_error)
            logging.info("Evaluated prediction id=%s symbol=%s target=%s predicted=%.4f actual=%.4f pct_error=%s",
                         prediction_id, symbol, target_date, predicted_price, actual, f"{pct_error:.2f}%" if pct_error is not None else "N/A")
            # avoid hitting rate limits
            time.sleep(1)

    conn.close()
    logging.info("Run complete.")


if __name__ == "__main__":
    main()
