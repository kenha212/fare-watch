# Fare Watch — SYD ⇄ SGN

A small script that checks a round-trip fare every few days, keeps a
price history, and emails you when it looks like a good time to buy.
Runs for free on GitHub Actions — no server or laptop needs to stay on.

## What it needs

1. **A GitHub repo.** Push this `fare-watch/` folder to a repo (public
   or private both work with Actions).

2. **A RapidAPI key, subscribed to "Google Flights Live API" (free).**
   - Sign up / log in at https://rapidapi.com
   - Go to https://rapidapi.com/mtnrabi/api/google-flights-live-api and
     subscribe to the free **BASIC** plan.
   - Copy your key from the "Authorization" tab (`X-RapidAPI-Key`).
   - Note: this returns real, live Google Flights fare data, including
     Google's own "low / typical / high" verdict for the route and
     period — nicer than a sandbox, since it's a real pass-through
     rather than test data.

3. **A Gmail app password (for sending the email).**
   - Turn on 2-Step Verification on the Gmail account you want to send
     from: https://myaccount.google.com/security
   - Create an app password: https://myaccount.google.com/apppasswords
   - Use that 16-character password, not your normal Gmail password.

4. **Add three repo secrets** (Settings → Secrets and variables →
   Actions → New repository secret):
   - `RAPIDAPI_KEY`
   - `SMTP_USER` — the Gmail address you're sending from
   - `SMTP_PASS` — the app password from step 3

## Configure your trip

Edit `config.json`:

```json
{
  "origin": "SYD",
  "destination": "SGN",
  "depart_date": "2026-11-20",
  "return_date": "2027-01-08",
  "target_price_aud": 450,
  "currency": "AUD",
  "email_to": "huykenny212@gmail.com"
}
```

## Schedule

`.github/workflows/fare-watch.yml` runs every 3 days at 22:00 UTC
(~9am Sydney time). Change the `cron` line to adjust — cron fields are
`minute hour day month weekday`, always in UTC.

You can also trigger a run manually any time from the Actions tab
("Run workflow" button) to test it.

## What "buy signal" means

Same logic as the Ticket Watch app:
- **Buy now** — at/under your target price, or near the lowest fare
  logged within the typical 6–8 week booking window, or inside 2 weeks
  of departure.
- **Prices rising** — latest fare is higher than the last check.
- **Keep watching** — no strong signal yet.

You'll only get an email for "Buy now" or "Prices rising" — not every
check, to avoid spamming your inbox.

## Local test run

```bash
pip install -r requirements.txt
RAPIDAPI_KEY=xxx SMTP_USER=you@gmail.com SMTP_PASS=xxxxxxxxxxxxxxxx \
  python fare_watch.py
```

Without `SMTP_USER`/`SMTP_PASS` set, it prints the message instead of
emailing — useful for testing the fare fetch and signal logic first.# fare-watch
