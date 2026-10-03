"""
BSE Results Alert Bot - poller.py (patched version)

What changed compared to the old version:
  1. Only companies CURRENTLY in watchlist.txt get alerts (removed ones stop).
  2. Company names / headlines are made Telegram-safe (fixes "&" failures).
  3. Dates use Indian time (IST), not the server's UTC clock.
  4. Looks back over yesterday + today, so a short outage never loses a filing.
  5. Retries BSE a few times before giving up.
  6. Sends you a Telegram warning if the bot breaks, and a message when it recovers.
  7. Alerts now include the filing time and a link to the PDF.
"""

import html
import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

import requests
from bse import BSE
from bse.constants import CATEGORY

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
WATCHLIST_FILE = "watchlist.txt"
SCRIPCODE_CACHE_FILE = "scripcode_cache.json"
SEEN_FILE = "seen.json"
HEALTH_FILE = "health.json"

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

IST = timezone(timedelta(hours=5, minutes=30))  # Indian Standard Time
LOOKBACK_DAYS = 1           # 1 = check yesterday and today
MAX_PAGES = 60              # safety limit on BSE pages per run
BSE_RETRIES = 3             # attempts per BSE page before giving up
FAILURE_ALERT_EVERY = 12    # while broken, repeat the warning every 12th failed run
ATTACHMENT_URL = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/{}"


# ---------------------------------------------------------------------------
# Small helpers for files
# ---------------------------------------------------------------------------
def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def load_watchlist():
    """Return the active entries in watchlist.txt (lines starting with # are ignored)."""
    entries = []
    with open(WATCHLIST_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            entries.append(line.upper())
    return entries


def now_ist_text():
    return datetime.now(IST).strftime("%d-%m-%Y %H:%M IST")


def hide_token(text):
    """Make sure the Telegram token never ends up in logs or in health.json."""
    return text.replace(TELEGRAM_BOT_TOKEN, "***TOKEN***")


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    for _ in range(3):
        try:
            resp = requests.post(url, data=payload, timeout=15)
        except requests.RequestException as e:
            raise RuntimeError(hide_token(f"Could not reach Telegram: {e}")) from None

        if resp.status_code == 429:  # Telegram says "slow down"
            try:
                wait = int(resp.json().get("parameters", {}).get("retry_after", 5))
            except ValueError:
                wait = 5
            print(f"Telegram rate limit hit, waiting {wait}s")
            time.sleep(wait + 1)
            continue

        if not resp.ok:
            raise RuntimeError(f"Telegram error {resp.status_code}: {resp.text}")

        time.sleep(1)  # stay well under Telegram's limits when sending several alerts
        return
    raise RuntimeError("Telegram kept rate-limiting after 3 attempts")


# ---------------------------------------------------------------------------
# Watchlist -> BSE codes
# ---------------------------------------------------------------------------
def resolve_watchlist(bse, entries, old_cache):
    """
    Build the set of BSE codes to watch from the CURRENT watchlist only.
    Numeric entries are used as-is. Ticker entries (e.g. TCS) are looked up
    once and remembered in scripcode_cache.json. Anything no longer in the
    watchlist is dropped from the cache automatically.
    """
    codes = set()
    new_cache = {}
    for entry in entries:
        if entry.isdigit():
            codes.add(entry)
            continue
        code = old_cache.get(entry)
        if code is None:
            try:
                code = str(bse.getScripCode(entry))
                print(f"Looked up BSE code for {entry}: {code}")
            except Exception as e:
                print(f"WARNING: could not find a BSE code for '{entry}': {e}")
                continue
        new_cache[entry] = str(code)
        codes.add(str(code))
    return codes, new_cache


# ---------------------------------------------------------------------------
# BSE
# ---------------------------------------------------------------------------
def row_id(row):
    news_id = row.get("NEWSID")
    if news_id:
        return str(news_id)
    return f"{row.get('SCRIP_CD', '')}|{row.get('News_submission_dt', '')}|{row.get('NEWSSUB', '')}"


def fetch_page(bse, page_no, from_date, to_date):
    for attempt in range(1, BSE_RETRIES + 1):
        try:
            return bse.announcements(
                page_no=page_no,
                from_date=from_date,
                to_date=to_date,
                category=CATEGORY.RESULT,
            )
        except Exception as e:
            if attempt == BSE_RETRIES:
                raise
            wait = 10 * attempt
            print(f"BSE request failed (page {page_no}, attempt {attempt}): {e}. Retrying in {wait}s")
            time.sleep(wait)


def fetch_all_results(bse, from_date, to_date):
    """Fetch every 'Result' announcement between the two dates, page by page."""
    all_rows = []
    ids_so_far = set()
    total_count = None

    for page_no in range(1, MAX_PAGES + 1):
        data = fetch_page(bse, page_no, from_date, to_date) or {}
        rows = data.get("Table") or []
        meta = data.get("Table1") or []

        if meta and total_count is None:
            try:
                total_count = int(meta[0].get("ROWCNT"))
            except (TypeError, ValueError):
                total_count = None

        new_rows = [r for r in rows if row_id(r) not in ids_so_far]
        if not new_rows:
            break  # no more pages
        for r in new_rows:
            ids_so_far.add(row_id(r))
            all_rows.append(r)

        if total_count is not None and len(all_rows) >= total_count:
            break
    else:
        print(f"WARNING: stopped after {MAX_PAGES} pages (safety limit).")

    return all_rows, total_count


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------
def build_message(row, code):
    name = html.escape(str(row.get("SLONGNAME") or code), quote=False)
    headline = html.escape(str(row.get("NEWSSUB") or "Result announcement"), quote=False)
    filed_at = str(row.get("News_submission_dt") or "").replace("T", " ")[:16]

    lines = [f"📊 <b>{name}</b> (BSE: {code})", headline]
    if filed_at:
        lines.append(f"🕒 Filed: {filed_at} IST")
    attachment = row.get("ATTACHMENTNAME")
    if attachment:
        link = html.escape(ATTACHMENT_URL.format(attachment))
        lines.append(f'📎 <a href="{link}">Open filing (PDF)</a>')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Health (warn you when the bot breaks)
# ---------------------------------------------------------------------------
def record_success(health):
    failures = health.get("consecutive_failures", 0)
    if failures > 0:
        try:
            send_telegram(f"✅ BSE alert bot is working again (after {failures} failed run(s)).")
        except Exception as e:
            print(f"Could not send recovery message: {e}")
    return {"consecutive_failures": 0}


def record_failure(health, error_text):
    error_text = hide_token(error_text)
    failures = health.get("consecutive_failures", 0) + 1
    new_health = {
        "consecutive_failures": failures,
        "last_failure_at": now_ist_text(),
        "last_error": error_text[-500:],
    }
    if failures == 1 or failures % FAILURE_ALERT_EVERY == 0:
        short_error = html.escape(error_text.strip().splitlines()[-1][-300:])
        try:
            send_telegram(
                "⚠️ <b>BSE alert bot problem</b>\n"
                f"The last {failures} run(s) failed, so results alerts may be missed "
                "until this is fixed.\n"
                f"Error: <code>{short_error}</code>"
            )
        except Exception as e:
            print(f"Could not send failure warning: {e}")
    return new_health


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    seen = set(load_json(SEEN_FILE, []))
    health = load_json(HEALTH_FILE, {"consecutive_failures": 0})
    old_cache = load_json(SCRIPCODE_CACHE_FILE, {})
    new_cache = old_cache
    exit_code = 0

    try:
        entries = load_watchlist()
        today = datetime.now(IST).replace(tzinfo=None)
        from_date = today - timedelta(days=LOOKBACK_DAYS)
        print(f"Watchlist: {len(entries)} active entries.")
        print(f"Checking results filed from {from_date:%d-%m-%Y} to {today:%d-%m-%Y} (IST).")

        with BSE(download_folder="bse_downloads") as bse:
            watch_codes, new_cache = resolve_watchlist(bse, entries, old_cache)
            rows, total_count = fetch_all_results(bse, from_date, today)

        print(f"Fetched {len(rows)} results announcements from BSE (reported total: {total_count}).")

        matched = sent = failed = 0
        for row in rows:
            code = str(row.get("SCRIP_CD", ""))
            if code not in watch_codes:
                continue
            matched += 1
            ann_id = row_id(row)
            if ann_id in seen:
                continue
            try:
                send_telegram(build_message(row, code))
                seen.add(ann_id)  # mark as sent straight away
                sent += 1
                print(f"Sent alert for {code}: {row.get('NEWSSUB', '')}")
            except Exception as e:
                failed += 1
                print(f"FAILED to send alert for {code}: {e}")

        print(f"Watchlist matches: {matched}. New alerts sent: {sent}. Failed: {failed}.")
        if failed:
            raise RuntimeError(f"{failed} alert(s) could not be sent; they will be retried next run.")

        health = record_success(health)

    except Exception:
        error_text = traceback.format_exc()
        print(hide_token(error_text))
        health = record_failure(health, error_text)
        exit_code = 1

    finally:
        save_json(SEEN_FILE, sorted(seen))
        save_json(HEALTH_FILE, health)
        if new_cache != old_cache:
            save_json(SCRIPCODE_CACHE_FILE, new_cache)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
