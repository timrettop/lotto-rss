"""
Lottery RSS Feed Generator
- Fetches Powerball & Mega Millions from RapidAPI on smart schedule (~43 calls/month)
- Monitors IMAP inbox for ticket purchase confirmation emails
- Serves an RSS feed consumable by DakBoard
"""

import os
import json
import imaplib
import email
import logging
import re
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from email.header import decode_header

import random
import requests
from flask import Flask, Response, request
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

class _SuppressHealthChecks(logging.Filter):
    """Drop successful GET /health log lines from Werkzeug's access log."""
    def filter(self, record: logging.LogRecord) -> bool:
        return not ('"GET /health' in record.getMessage() and '" 200 ' in record.getMessage())

logging.getLogger("werkzeug").addFilter(_SuppressHealthChecks())

# ── Config ────────────────────────────────────────────────────────────────────
RAPIDAPI_KEY  = os.environ.get("RAPIDAPI_KEY", "")
RAPIDAPI_HOST = "usa-lottery-result-all-state-api.p.rapidapi.com"

IMAP_HOST   = os.environ.get("IMAP_HOST", "imap.gmail.com")
IMAP_PORT   = int(os.environ.get("IMAP_PORT", "993"))
IMAP_USER   = os.environ.get("IMAP_USER", "")
IMAP_PASS   = os.environ.get("IMAP_PASS", "")
IMAP_FOLDER = os.environ.get("IMAP_FOLDER", "INBOX")

DATA_DIR      = Path("/app/data")
JACKPOT_CACHE = DATA_DIR / "jackpots.json"
TICKETS_FILE  = DATA_DIR / "tickets.json"

PORT            = int(os.environ.get("PORT", "8080"))
RSS_TITLE       = os.environ.get("RSS_TITLE", "🎰 Lottery Tracker")
RSS_LINK        = os.environ.get("RSS_LINK", "http://localhost:8080/rss")
RSS_DESCRIPTION = os.environ.get("RSS_DESCRIPTION", "Upcoming Powerball &amp; Mega Millions draws")

NEWS_FEED_URL    = os.environ.get("NEWS_FEED_URL", "")        # Any RSS URL, leave blank to disable
NEWS_FEED_COUNT  = int(os.environ.get("NEWS_FEED_COUNT", "2"))  # How many news items to append
NEWS_FETCH_HOURS = int(os.environ.get("NEWS_FETCH_HOURS", "4"))    # How often to refresh news feed
NEWS_CACHE       = DATA_DIR / "news.json"

API_KEY = os.environ.get("API_KEY", "")   # Required query param on /rss. Leave blank to disable.

app = Flask(__name__)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── Game definitions ──────────────────────────────────────────────────────────
# gameID is state-scoped (IL). Draw days are 0=Mon ... 6=Sun (Python weekday())
GAMES = {
    "powerball": {
        "gameID": 136,
        "name": "Powerball",
        "emoji": "🔴",
        "draw_weekdays": {0, 2, 5},   # Mon, Wed, Sat
        "timezone": "America/New_York",
    },
    "megamillions": {
        "gameID": 137,
        "name": "Mega Millions",
        "emoji": "💛",
        "draw_weekdays": {1, 4},       # Tue, Fri
        "timezone": "America/Detroit",
    },
}


def should_fetch_today(draw_weekdays: set) -> bool:
    """
    Fetch on draw days (day-of) and the day after each draw.
    Skip all other days to stay under 50 API calls/month.
    """
    today = datetime.now(timezone.utc).weekday()  # 0=Mon, 6=Sun
    yesterday = (today - 1) % 7
    return today in draw_weekdays or yesterday in draw_weekdays


# ── API Fetcher ───────────────────────────────────────────────────────────────

def fetch_game(key: str, game: dict) -> dict | None:
    """Fetch latest draw result + next draw info from RapidAPI."""
    if not RAPIDAPI_KEY:
        log.warning("RAPIDAPI_KEY not set — skipping fetch")
        return None
    try:
        url = f"https://{RAPIDAPI_HOST}/lottery-results/game-result"
        headers = {
            "x-rapidapi-key": RAPIDAPI_KEY,
            "x-rapidapi-host": RAPIDAPI_HOST,
        }
        resp = requests.get(url, headers=headers, params={"gameID": game["gameID"]}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "success" and data.get("data"):
            log.info(f"Fetched {key}: jackpot={data['data'].get('nextJackpot')}")
            return data["data"]
        else:
            log.warning(f"Unexpected response for {key}: {data}")
            return None
    except Exception as e:
        log.error(f"Failed to fetch {key}: {e}")
        return None


def run_scheduled_fetch():
    """Run at 7:15 AM daily. Only fetches games whose schedule warrants it today."""
    log.info("Scheduled fetch triggered")
    cache = load_cache()
    changed = False

    for key, game in GAMES.items():
        if should_fetch_today(game["draw_weekdays"]):
            log.info(f"Fetching {key} (scheduled)")
            result = fetch_game(key, game)
            if result:
                cache[key] = {
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "data": result,
                }
                changed = True
        else:
            log.info(f"Skipping {key} today (not a fetch day)")

    if changed:
        save_cache(cache)

    # Always check email regardless of lottery fetch
    check_email_for_tickets()


def load_cache() -> dict:
    if JACKPOT_CACHE.exists():
        with open(JACKPOT_CACHE) as f:
            return json.load(f)
    return {}


def save_cache(cache: dict):
    with open(JACKPOT_CACHE, "w") as f:
        json.dump(cache, f, indent=2)


# ── Next Draw Date Calculator ─────────────────────────────────────────────────

def get_next_draw_date(game: dict, api_data: dict) -> datetime | None:
    """
    Use nextDrawDate from API if it's in the future.
    Fall back to calculating from drawDays if it's stale.
    """
    tz = pytz.timezone(game["timezone"])
    now = datetime.now(tz)

    # Try API-provided next draw date first
    raw = api_data.get("nextDrawDate")
    if raw:
        try:
            naive = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
            api_dt = tz.localize(naive)
            if api_dt > now:
                return api_dt
        except ValueError:
            pass

    # Fall back: calculate next draw day from drawDays in gameDetails
    draw_days = game["draw_weekdays"]
    draw_time_str = api_data.get("gameDetails", {}).get("drawTime", "22:59:00")
    h, m, s = [int(x) for x in draw_time_str.split(":")]

    for days_ahead in range(1, 8):
        candidate = now + timedelta(days=days_ahead)
        if candidate.weekday() in draw_days:
            next_draw = tz.localize(
                datetime(candidate.year, candidate.month, candidate.day, h, m, s)
            )
            return next_draw

    return None


# ── Ticket Tracker ────────────────────────────────────────────────────────────

def load_tickets() -> dict:
    if TICKETS_FILE.exists():
        with open(TICKETS_FILE) as f:
            return json.load(f)
    return {"powerball": [], "megamillions": []}


def save_tickets(tickets: dict):
    with open(TICKETS_FILE, "w") as f:
        json.dump(tickets, f, indent=2)


def check_email_for_tickets():
    """
    Poll IMAP inbox for forwarded lottery confirmation emails.
    Extracts draw dates and records which games have tickets purchased.
    """
    if not IMAP_USER or not IMAP_PASS:
        log.info("IMAP not configured — skipping ticket check")
        return

    tickets = load_tickets()
    changed = False

    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(IMAP_USER, IMAP_PASS)
        mail.select(IMAP_FOLDER)

        searches = [
            ("powerball",    '(UNSEEN SUBJECT "powerball")'),
            ("megamillions", '(UNSEEN SUBJECT "mega millions")'),
        ]

        for game_key, search_query in searches:
            _, msg_ids = mail.search(None, search_query)
            ids = msg_ids[0].split() if msg_ids[0] else []
            for uid in ids:
                try:
                    _, msg_data = mail.fetch(uid, "(RFC822)")
                    if not msg_data or not msg_data[0]:
                        log.warning(f"No data returned for message {uid}")
                        continue
                    msg = email.message_from_bytes(bytes(msg_data[0][1]))
                    body = extract_body(msg)
                    subject = decode_mime_header(msg.get("Subject", ""))
                    full_text = subject + " " + body

                    dates = extract_dates(full_text)
                    for d in dates:
                        if d not in tickets.get(game_key, []):
                            tickets.setdefault(game_key, []).append(d)
                            changed = True
                            log.info(f"Recorded {game_key} ticket for {d}")

                    mail.store(uid, "+FLAGS", "\\Seen")
                except Exception as e:
                    log.error(f"Error processing email {uid}: {e}")

        mail.logout()

    except Exception as e:
        log.error(f"IMAP error: {e}")

    if changed:
        save_tickets(tickets)


def decode_mime_header(value: str) -> str:
    parts = decode_header(value)
    result = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            result += part.decode(enc or "utf-8", errors="ignore")
        else:
            result += part
    return result


def extract_body(msg) -> str:
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    body += part.get_payload(decode=True).decode("utf-8", errors="ignore")
                except Exception:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
        except Exception:
            pass
    return body


def extract_dates(text: str) -> list:
    """
    Extract draw dates ONLY from Draw Date and Time: lines in Illinois Lottery emails.
    Format: Draw Date and Time: * 9:45PM, 03/10/2026 *
    Ignores all other dates in the email (purchase date, timestamps, etc.)
    """
    found = set()

    # Match "Draw Date and Time:" followed by optional asterisks/whitespace,
    # then a time like 9:45PM, then MM/DD/YYYY.
    # Handles plain text:  "Draw Date and Time: * 9:45PM, 03/10/2026 *"
    # Handles decoded HTML with extra whitespace between elements.
    pattern = re.compile(
        r'Draw Date and Time\s*:[\s\*]*'
        r'[\d:]+\s*[AP]M\s*,\s*'
        r'(\d{1,2})/(\d{1,2})/(\d{4})',
        re.IGNORECASE
    )

    for m in pattern.finditer(text):
        try:
            d = date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
            found.add(d.isoformat())
        except ValueError:
            log.warning(f"Could not parse draw date from: {m.group(0)}")

    if not found:
        log.warning("No Draw Date and Time: lines found in email — no dates extracted")

    return list(found)


def has_ticket(game_key: str, draw_dt: datetime | None) -> bool:
    if not draw_dt:
        return False
    tickets = load_tickets()
    return draw_dt.strftime("%Y-%m-%d") in tickets.get(game_key, [])


# ── News Feed Fetcher ─────────────────────────────────────────────────────────

def fetch_news_feed():
    """Fetch and cache items from the configured external RSS feed."""
    if not NEWS_FEED_URL:
        return

    try:
        resp = requests.get(NEWS_FEED_URL, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()

        import xml.etree.ElementTree as ET
        root = ET.fromstring(resp.content)

        ATOM_NS = "http://www.w3.org/2005/Atom"
        ns = {"atom": ATOM_NS}

        # Handle both RSS <item> and Atom <entry> — use explicit len() check
        # to avoid the ElementTree "truth value of element is ambiguous" warning
        rss_items  = root.findall(".//item")
        atom_items = root.findall(".//atom:entry", ns)
        items = rss_items if len(rss_items) > 0 else atom_items

        def find_el(item, tag):
            """Find element by plain tag first, then namespaced atom: tag."""
            el = item.find(tag)
            if el is None:
                el = item.find(f"atom:{tag}", ns)
            return el

        def get_text(item, tag) -> str:
            el = find_el(item, tag)
            return el.text.strip() if el is not None and el.text else ""

        def get_link(item) -> str:
            # RSS: <link>url</link>
            el = item.find("link")
            if el is not None and el.text:
                return el.text.strip()
            # Atom: <link href="url"/>
            el = item.find(f"atom:link", ns)
            if el is not None:
                return el.get("href", "").strip()
            return ""

        results = []
        for item in items[:20]:  # cache up to 20, sample randomly at serve time
            title = get_text(item, "title")
            link  = get_link(item)
            pub   = get_text(item, "pubDate") or get_text(item, "published") or get_text(item, "updated")

            if title:
                results.append({"title": title, "link": link, "pubDate": pub})

        with open(NEWS_CACHE, "w") as f:
            json.dump({"fetched_at": datetime.now(timezone.utc).isoformat(), "items": results}, f, indent=2)
        log.info(f"News feed cached: {len(results)} items from {NEWS_FEED_URL}")

    except Exception as e:
        log.error(f"Failed to fetch news feed: {e}")


def load_news_items() -> list:
    """Return NEWS_FEED_COUNT random items from the cached news feed."""
    if not NEWS_FEED_URL or not NEWS_CACHE.exists():
        return []
    try:
        with open(NEWS_CACHE) as f:
            data = json.load(f)
        items = data.get("items", [])
        if len(items) <= NEWS_FEED_COUNT:
            return items
        return random.sample(items, NEWS_FEED_COUNT)
    except Exception as e:
        log.error(f"Failed to load news cache: {e}")
        return []


# ── RSS Builder ───────────────────────────────────────────────────────────────

def format_jackpot(raw: str | None) -> str:
    if not raw:
        return "TBD"
    return str(raw).strip().title()


def build_rss() -> str:
    cache = load_cache()
    now_utc = datetime.now(timezone.utc)
    items = []

    for key, game in GAMES.items():
        game_cache = cache.get(key, {})
        api_data = game_cache.get("data", {})

        next_draw = get_next_draw_date(game, api_data)
        jackpot   = format_jackpot(api_data.get("nextJackpot"))
        cash      = format_jackpot(api_data.get("nextCash"))
        ticket    = has_ticket(key, next_draw)

        if next_draw:
            draw_str     = next_draw.strftime("%a %b %-d @ %-I:%M %p %Z")
            draw_date_iso = next_draw.strftime("%Y-%m-%d")
        else:
            draw_str      = "Next draw date unknown"
            draw_date_iso = "unknown"

        ticket_str  = "✅ Ticket Purchased" if ticket else "❌ No Ticket Yet"
        ticket_flag = "bought" if ticket else "not-bought"

        title = f"{game['emoji']} {game['name']} — {jackpot} — {draw_str} — {ticket_str}"

        description = (
            f"<b>{game['name']}</b><br/>"
            f"Next Draw: {draw_str}<br/>"
            f"Jackpot: {jackpot} (Cash: {cash})<br/>"
            f"Ticket: {ticket_str}"
        )

        fetched_at = game_cache.get("fetched_at", now_utc.isoformat())

        items.append({
            "title": title,
            "description": description,
            "guid": f"{key}-{draw_date_iso}",
            "pub_date": fetched_at,
            "ticket_flag": ticket_flag,
            "game": key,
        })

    # Sort by next draw date (soonest first)
    def sort_key(item):
        return item["guid"]

    items.sort(key=sort_key)

    rss_items = ""
    for item in items:
        pub_dt = datetime.fromisoformat(item["pub_date"].replace("Z", "+00:00"))
        pub_rfc = pub_dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        rss_items += f"""
    <item>
      <title><![CDATA[{item['title']}]]></title>
      <description><![CDATA[{item['description']}]]></description>
      <guid isPermaLink="false">{item['guid']}</guid>
      <pubDate>{pub_rfc}</pubDate>
      <category>{item['ticket_flag']}</category>
    </item>"""

    # Append random news items after lottery entries
    for news in load_news_items():
        pub = news.get("pubDate", now_utc.strftime("%a, %d %b %Y %H:%M:%S +0000"))
        link = news.get("link", "")
        safe_title = news["title"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        rss_items += f"""
    <item>
      <title><![CDATA[📰 {news["title"]}]]></title>
      <description><![CDATA[{safe_title}]]></description>
      <guid isPermaLink="{"true" if link else "false"}">{link or safe_title[:80]}</guid>
      <pubDate>{pub}</pubDate>
      <category>news</category>
    </item>"""

    now_rfc = now_utc.strftime("%a, %d %b %Y %H:%M:%S +0000")

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>{RSS_TITLE}</title>
    <link>{RSS_LINK}</link>
    <description>{RSS_DESCRIPTION}</description>
    <lastBuildDate>{now_rfc}</lastBuildDate>
    <ttl>720</ttl>
    {rss_items}
  </channel>
</rss>"""



def dead_rss() -> str:
    """Return a convincing but empty RSS feed for unauthenticated requests."""
    return """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Feed Unavailable</title>
    <link>http://localhost/</link>
    <description>No data available</description>
    <lastBuildDate>Thu, 01 Jan 2015 00:00:00 +0000</lastBuildDate>
    <ttl>1440</ttl>
  </channel>
</rss>"""


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/rss")
def rss_feed():
    if API_KEY and request.args.get("api_key") != API_KEY:
        return Response(dead_rss(), mimetype="application/rss+xml")
    return Response(build_rss(), mimetype="application/rss+xml")


@app.route("/health")
def health():
    if API_KEY and request.args.get("api_key") != API_KEY:
        return {"status": "ok", "cached_games": [], "tickets": {}, "fetch_schedule": {}}, 200
    cache = load_cache()
    tickets = load_tickets()
    return {
        "status": "ok",
        "cached_games": list(cache.keys()),
        "tickets": tickets,
        "fetch_schedule": {
            key: "fetches today" if should_fetch_today(game["draw_weekdays"]) else "skip today"
            for key, game in GAMES.items()
        },
    }


@app.route("/fetch", methods=["POST"])
def manual_fetch():
    """Manual trigger — useful for initial setup or testing."""
    cache = load_cache()
    results = {}
    for key, game in GAMES.items():
        result = fetch_game(key, game)
        if result:
            cache[key] = {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "data": result,
            }
            results[key] = "ok"
        else:
            results[key] = "failed"
    save_cache(cache)
    check_email_for_tickets()
    return {"status": "done", "results": results}


@app.route("/fetch/news", methods=["POST"])
def manual_fetch_news():
    """Manually trigger a news feed refresh, independent of lottery schedule."""
    if not NEWS_FEED_URL:
        return {"status": "skipped", "reason": "NEWS_FEED_URL not configured"}, 400
    fetch_news_feed()
    try:
        with open(NEWS_CACHE) as f:
            data = json.load(f)
        count = len(data.get("items", []))
    except Exception:
        count = 0
    return {"status": "done", "items_cached": count, "source": NEWS_FEED_URL}


@app.route("/ticket/<game>/<draw_date>", methods=["POST"])
def add_ticket(game: str, draw_date: str):
    """Manually record a ticket purchase. game=powerball|megamillions, draw_date=YYYY-MM-DD"""
    if game not in GAMES:
        return {"error": "unknown game"}, 400
    try:
        date.fromisoformat(draw_date)
    except ValueError:
        return {"error": "invalid date, use YYYY-MM-DD"}, 400

    tickets = load_tickets()
    if draw_date not in tickets.get(game, []):
        tickets.setdefault(game, []).append(draw_date)
        save_tickets(tickets)
    return {"status": "ok", "game": game, "draw_date": draw_date}


@app.route("/ticket/<game>/<draw_date>", methods=["DELETE"])
def remove_ticket(game: str, draw_date: str):
    """Remove a ticket record."""
    tickets = load_tickets()
    if draw_date in tickets.get(game, []):
        tickets[game].remove(draw_date)
        save_tickets(tickets)
    return {"status": "ok"}


# ── Scheduler ─────────────────────────────────────────────────────────────────

def start_scheduler():
    scheduler = BackgroundScheduler(timezone="America/Chicago")

    # Job 1: Lottery data — runs once daily at 7:15 AM CT
    # Only fires API calls on draw-day / day-after schedule (~43 calls/month)
    scheduler.add_job(run_scheduled_fetch, "cron", hour=7, minute=15)

    # Job 2: News feed — runs independently every NEWS_FETCH_HOURS hours
    # Completely separate from lottery schedule, no API limit concerns
    if NEWS_FEED_URL:
        scheduler.add_job(fetch_news_feed, "interval", hours=NEWS_FETCH_HOURS)
        log.info(f"News feed scheduled every {NEWS_FETCH_HOURS}h from {NEWS_FEED_URL}")
    else:
        log.info("News feed disabled (NEWS_FEED_URL not set)")

    scheduler.start()
    log.info(f"Scheduler started — lottery fetch at 7:15 AM CT, serving on port {PORT}")


# ── Startup ───────────────────────────────────────────────────────────────────
# Runs at import time so both Gunicorn and direct `python app.py` trigger it.
start_scheduler()
if not JACKPOT_CACHE.exists():
    log.info("No lottery cache found — running initial fetch on startup")
    run_scheduled_fetch()
if NEWS_FEED_URL and not NEWS_CACHE.exists():
    log.info("No news cache found — fetching news feed on startup")
    fetch_news_feed()

if __name__ == "__main__":
    # Dev only — Gunicorn does not use this block
    app.run(host="0.0.0.0", port=PORT, debug=False)