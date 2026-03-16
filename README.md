# Lottery RSS Feed

Serves an RSS feed for DakBoard showing upcoming Powerball & Mega Millions draws,
jackpot amounts, and whether you've purchased a ticket. Optionally mixes in news
headlines from any RSS/Atom feed.

## Quick Start

```bash
git clone <your-repo>
cd lottery-rss
cp docker-compose.yml docker-compose.yml.bak   # optional backup
```

Edit `docker-compose.yml` and fill in:
- `RAPIDAPI_KEY` — from your RapidAPI account
- `IMAP_USER` / `IMAP_PASS` — Gmail address + App Password (see below)
- `RSS_LINK` — set to `http://YOUR_SERVER_IP:8585/rss`

```bash
docker compose up -d --build
```

Trigger an initial data fetch (bypasses the daily schedule):
```bash
curl -X POST http://localhost:8585/fetch
```

Your RSS feed is now live at:
```
http://YOUR_SERVER_IP:8585/rss
```

The port defaults to `8585` and is configurable via the `PORT` environment variable.

---

## API Call Budget

This service uses the `usa-lottery-result-all-state-api` on RapidAPI.
Fetches only run on draw days and the day after each draw to pick up results.

| Game | Draw days | Fetch days | Calls/week |
|---|---|---|---|
| Powerball | Mon / Wed / Sat | Mon, Tue, Wed, Thu, Sat, Sun | 6 |
| Mega Millions | Tue / Fri | Tue, Wed, Fri, Sat | 4 (Tue/Wed shared with Powerball) |
| **Total** | | | **~10/week → ~43/month** |

Stays under the 50 calls/month free tier limit.

Game IDs used (Illinois state scope):
- Powerball: `136`
- Mega Millions: `137`

---

## News Feed (Optional)

Set `NEWS_FEED_URL` to any RSS or Atom feed URL to mix headlines into the RSS output.

```yaml
- NEWS_FEED_URL=https://feeds.npr.org/1001/rss.xml
- NEWS_FEED_COUNT=2        # number of items shown per refresh
- NEWS_FETCH_HOURS=4       # how often to refresh the news cache
```

Leave `NEWS_FEED_URL` blank to disable. The news scheduler runs completely
independently from the lottery scheduler and uses no RapidAPI calls.

Recommended feeds:
| Source | URL |
|---|---|
| NPR | `https://feeds.npr.org/1001/rss.xml` |
| BBC US | `https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml` |
| Reuters | `https://feeds.reuters.com/reuters/topNews` |

---

## Email / Ticket Tracking

Forward your Illinois Lottery confirmation emails to a dedicated Gmail address.

### Gmail App Password Setup
1. Enable 2-factor authentication on your Gmail account
2. Go to https://myaccount.google.com/apppasswords
3. Create an app password for "Mail"
4. Use that 16-character password as `IMAP_PASS`

### Email Forwarding
In your main email client, create a filter:
- From: `no-reply@illinoislottery.com` (or your lottery's sender address)
- Action: Forward to your dedicated Gmail address

The service checks for **unread emails** with "powerball" or "mega millions"
in the subject line. It extracts the draw date specifically from the
`Draw Date and Time:` line in the email body, ignoring all other dates
(purchase date, etc.), then marks emails read after processing.

### Manual Ticket Entry
If email parsing isn't working, you can record tickets manually:

```bash
# Record a ticket purchase
curl -X POST http://localhost:8585/ticket/powerball/2026-03-08

# Remove a ticket record
curl -X DELETE http://localhost:8585/ticket/powerball/2026-03-08

# Mega Millions
curl -X POST http://localhost:8585/ticket/megamillions/2026-03-10
```

---

## DakBoard Setup

1. In DakBoard, add a new **RSS Feed** widget
2. Set the URL to `http://YOUR_SERVER_IP:8585/rss`
3. Set refresh interval to **12 hours** (the feed has `<ttl>720</ttl>` set)
4. Display **title only** for the cleanest look — titles are self-contained:
   ```
   🔴 Powerball — $20 Million — Wed Mar 8 @ 10:59 PM ET — ✅ Ticket Purchased
   💛 Mega Millions — $496 Million — Fri Mar 10 @ 11:00 PM ET — ❌ No Ticket Yet
   📰 Some news headline here
   ```

**Note:** If DakBoard reports "Invalid RSS/XML feed", check that `RSS_DESCRIPTION`
in `docker-compose.yml` uses `&amp;` instead of `&` for any ampersands:
```yaml
- RSS_DESCRIPTION=Upcoming Powerball &amp; Mega Millions draws
```

---

## Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/rss` | GET | RSS feed for DakBoard |
| `/health` | GET | JSON status — cache state, tickets, today's fetch schedule |
| `/fetch` | POST | Manually trigger lottery API fetch + email check (bypasses schedule) |
| `/fetch/news` | POST | Manually trigger a news feed refresh (independent of lottery schedule) |
| `/ticket/<game>/<date>` | POST | Record a ticket purchase (`date` = YYYY-MM-DD) |
| `/ticket/<game>/<date>` | DELETE | Remove a ticket record |

### Useful one-liners

```bash
# Check status
curl http://localhost:8585/health

# Force-refresh lottery data
curl -X POST http://localhost:8585/fetch

# Force-refresh news only
curl -X POST http://localhost:8585/fetch/news

# Check the raw RSS output
curl http://localhost:8585/rss
```

---

## Data Persistence

All data is stored in `./data/` (mounted as a Docker volume):

- `jackpots.json` — cached API responses, updated on fetch days
- `tickets.json` — purchased ticket records by game and draw date
- `news.json` — cached news feed items (pool of up to 20, random sample served)

Survives container restarts and rebuilds.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8585` | Port the service listens on |
| `RAPIDAPI_KEY` | *(required)* | Your RapidAPI key |
| `IMAP_HOST` | `imap.gmail.com` | IMAP server hostname |
| `IMAP_PORT` | `993` | IMAP port |
| `IMAP_USER` | *(required)* | Email address for IMAP login |
| `IMAP_PASS` | *(required)* | Gmail App Password |
| `IMAP_FOLDER` | `INBOX` | Mailbox folder to scan |
| `NEWS_FEED_URL` | *(blank)* | RSS/Atom URL for news headlines; leave blank to disable |
| `NEWS_FEED_COUNT` | `2` | Number of news items to show per RSS refresh |
| `NEWS_FETCH_HOURS` | `4` | How often to refresh the news cache (hours) |
| `RSS_TITLE` | `🎰 Lottery Tracker` | RSS feed title |
| `RSS_LINK` | *(required)* | Full URL to this feed, e.g. `http://192.168.1.x:8585/rss` |
| `RSS_DESCRIPTION` | `Upcoming Powerball &amp; Mega Millions draws` | RSS feed description (use `&amp;` for `&`) |

---

## Changing State

The game IDs (`136`, `137`) are scoped to Illinois. If you're in a different
state, call the game-list endpoint to find your IDs:

```bash
curl --request GET \
  --url 'https://usa-lottery-result-all-state-api.p.rapidapi.com/lottery-results/states/game-list?state=TX' \
  --header 'x-rapidapi-host: usa-lottery-result-all-state-api.p.rapidapi.com' \
  --header 'x-rapidapi-key: YOUR_KEY'
```

Then update the `gameID` values in the `GAMES` dict near the top of `app/app.py`.