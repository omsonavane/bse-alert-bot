import os
import json
import time
from datetime import datetime
import requests
from bse import BSE
from bse.constants import CATEGORY

WATCHLIST_FILE = "watchlist.txt"
SCRIPCODE_CACHE_FILE = "scripcode_cache.json"
SEEN_FILE = "seen.json"

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


def load_watchlist():
    symbols = []
    with open(WATCHLIST_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            symbols.append(line.upper())
    return symbols


def load_json_set_or_dict(path, as_set=False):
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
            return set(data) if as_set else data
    return set() if as_set else {}


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, data=payload, timeout=10)
    resp.raise_for_status()


def resolve_scripcodes(bse, symbols, cache):
    changed = False
    for symbol in symbols:
        if symbol.isdigit():
            # Already a numeric scrip code — no lookup needed.
            if symbol not in cache.values():
                cache[symbol] = symbol
                changed = True
            continue
        if symbol not in cache:
            try:
                code = bse.getScripCode(symbol)
                cache[symbol] = str(code)
                changed = True
                print(f"Resolved and cached scrip code for {symbol}: {code}")
            except Exception as e:
                print(f"Could not resolve scrip code for {symbol}: {e}")
    scripcode_to_symbol = {v: k for k, v in cache.items()}
    return cache, scripcode_to_symbol, changed

def fetch_all_results_today(bse, today, max_retries=3):
    all_rows = []
    page_no = 1
    while True:
        data = None
        for attempt in range(1, max_retries + 1):
            try:
                data = bse.announcements(
                    page_no=page_no,
                    from_date=today,
                    to_date=today,
                    category=CATEGORY.RESULT,
                )
                break
            except Exception as e:
                print(f"Attempt {attempt}/{max_retries} failed fetching page {page_no}: {e}")
                if attempt < max_retries:
                    wait = 5 * attempt
                    print(f"Retrying in {wait} seconds...")
                    time.sleep(wait)

        if data is None:
            print(f"Giving up on page {page_no} after {max_retries} attempts. "
                  f"Returning {len(all_rows)} rows fetched so far.")
            break

        rows = data.get("Table", [])
        all_rows.extend(rows)

        total_meta = data.get("Table1", [])
        total_count = total_meta[0].get("ROWCNT") if total_meta else None

        if not rows or total_count is None or len(all_rows) >= total_count:
            break
        page_no += 1
        if page_no > 50:
            print("Stopped after 50 pages as a safety limit.")
            break

    return all_rows


def main():
    symbols = load_watchlist()
    print(f"Watchlist has {len(symbols)} symbols.")

    scripcode_cache = load_json_set_or_dict(SCRIPCODE_CACHE_FILE, as_set=False)
    seen_ids = load_json_set_or_dict(SEEN_FILE, as_set=True)
    updated_ids = set(seen_ids)

    today = datetime.now()

    with BSE(download_folder="bse_downloads") as bse:
        scripcode_cache, scripcode_to_symbol, cache_changed = resolve_scripcodes(
            bse, symbols, scripcode_cache
        )

        rows = fetch_all_results_today(bse, today)
        print(f"Fetched {len(rows)} total results announcements for today (all BSE companies).")

        matched = 0
        for row in rows:
            row_scripcode = str(row.get("SCRIP_CD", ""))
            symbol = scripcode_to_symbol.get(row_scripcode)
            if symbol is None:
                continue

            matched += 1
            ann_id = str(row.get("NEWSID", "") or (row_scripcode, row.get("News_submission_dt", "")))
            headline = row.get("NEWSSUB", "Result announcement")

            if ann_id in seen_ids:
                continue

            company_name = row.get("SLONGNAME", symbol)
            message = f"📊 <b>{symbol}</b> ({company_name}) — New BSE results filing\n{headline}"
            try:
                send_telegram(message)
                print(f"Sent alert for {symbol}: {headline}")
            except Exception as e:
                print(f"Failed to send Telegram message for {symbol}: {e}")
                continue

            updated_ids.add(ann_id)

    print(f"{matched} of today's results announcements matched your watchlist.")

    save_json(SEEN_FILE, sorted(updated_ids))
    if cache_changed:
        save_json(SCRIPCODE_CACHE_FILE, scripcode_cache)


if __name__ == "__main__":
    main()
