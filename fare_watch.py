"""
Fare Watch — checks a round-trip fare, logs it, and emails you when it
looks like a good time to buy.

Data sources (two, merged together):
1. "Google Flights Live API" on RapidAPI (free BASIC tier) — good
   coverage generally, but structurally does not carry some low-cost
   carriers' fares (VietJet included).
   Subscribe: https://rapidapi.com/mtnrabi/api/google-flights-live-api
2. A Kiwi.com-based scraper API on RapidAPI — does carry VietJet, but
   its free tier caps at 200 requests/month, so this script only calls
   it once per day (tracked in digest_state.json), not every run.
   Subscribe to whichever Kiwi-flights listing you tested in the
   RapidAPI playground.

Both use the same RAPIDAPI_KEY (RapidAPI keys work across every API
you've subscribed to on your account) — no separate key needed.

NOTE on both endpoints: host, path, and headers were confirmed directly
from real RapidAPI playground requests/responses, not guessed.

Run manually:
    RAPIDAPI_KEY=... python fare_watch.py

Or let the included GitHub Actions workflow run it on a schedule.
"""

import os
import json
import smtplib
import ssl
import time
from email.mime.text import MIMEText
from datetime import date

import requests

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
HISTORY_PATH = os.path.join(os.path.dirname(__file__), "price_history.json")
STATE_PATH = os.path.join(os.path.dirname(__file__), "digest_state.json")

RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY")

GOOGLE_FLIGHTS_HOST = "google-flights-live-api.p.rapidapi.com"
GOOGLE_FLIGHTS_PATH = "/api/google_flights/roundtrip/v1"  # confirmed via playground snippet

KIWI_HOST = "kiwi-com-api-kiwi-com-flights-scraper.p.rapidapi.com"
KIWI_PATH = "/v1/flight-offers/return/search"  # confirmed via playground snippet
KIWI_FREE_TIER_MONTHLY_LIMIT = 200  # keep Kiwi calls to ~once/day to stay well under this

SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _post_with_retries(url, headers, body, max_attempts=3, timeout=60):
    """Shared retry helper. Returns a requests.Response, or None if every
    attempt timed out."""
    resp = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=timeout)
            return resp
        except requests.exceptions.Timeout:
            print(f"Attempt {attempt}/{max_attempts} timed out waiting on {url}.")
            if attempt == max_attempts:
                print("Giving up after repeated timeouts — will try again next scheduled run.")
                return None
            time.sleep(5 * attempt)  # 5s, then 10s before the next try
    return resp


def search_google_fares(cfg, limit=3):
    """Returns a list of up to `limit` cheapest itineraries from Google
    Flights (via RapidAPI), each as {"price", "airline", "verdict"},
    sorted cheapest first. Empty list on failure or no results.

    Note: Google Flights structurally does not carry some low-cost
    carriers' fares (VietJet included) — that's why search_kiwi_fares
    exists as a second source.
    """
    body = {
        "from_airport": cfg["origin"],
        "to_airport": cfg["destination"],
        "departure_date": cfg["depart_date"],
        "return_date": cfg["return_date"],
    }
    headers = {
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": GOOGLE_FLIGHTS_HOST,
        "Content-Type": "application/json",
    }
    url = f"https://{GOOGLE_FLIGHTS_HOST}{GOOGLE_FLIGHTS_PATH}"

    resp = _post_with_retries(url, headers, body)
    if resp is None:
        return []
    if resp.status_code == 404:
        raise SystemExit(
            f"Got a 404 from {GOOGLE_FLIGHTS_PATH} — check the RapidAPI playground "
            "to confirm the path hasn't changed."
        )
    resp.raise_for_status()
    offers = resp.json()
    if not offers:
        return []

    offers_sorted = sorted(offers, key=lambda o: o["total_price_as_number"])

    # Keep the cheapest offer per distinct airline, so the top few aren't
    # all the same carrier's slightly-different-time flights.
    seen_airlines = set()
    top = []
    for o in offers_sorted:
        airline = o.get("departure_flight_airline")
        if airline in seen_airlines:
            continue
        seen_airlines.add(airline)
        top.append({
            "price": o["total_price_as_number"],
            "airline": airline,
            "verdict": o.get("price_range_in_relation_to_other_periods"),
        })
        if len(top) >= limit:
            break
    return top


def search_kiwi_fares(cfg, limit=3):
    """Returns a list of up to `limit` cheapest itineraries from Kiwi.com
    (via RapidAPI), each as {"price", "airline", "verdict": None}, sorted
    cheapest first. Empty list on failure or no results.

    This source does carry VietJet and other budget carriers that
    Google Flights doesn't. Its free RapidAPI tier is capped at 200
    requests/month, so main() only calls this once per day, not on
    every 4-hourly run.
    """
    body = {
        "adults": 1,
        "currency": cfg.get("currency", "AUD"),
        "departure_date": cfg["depart_date"],
        "destination": cfg["destination"],
        "limit": 10,
        "locale": "en",
        "max_stopovers": 1,
        "origin": cfg["origin"],
        "return_date": cfg["return_date"],
    }
    headers = {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": KIWI_HOST,
        "Content-Type": "application/json",
    }
    url = f"https://{KIWI_HOST}{KIWI_PATH}"

    resp = _post_with_retries(url, headers, body)
    if resp is None:
        return []
    if resp.status_code == 404:
        raise SystemExit(
            f"Got a 404 from {KIWI_PATH} — check the RapidAPI playground "
            "to confirm the path hasn't changed."
        )
    resp.raise_for_status()
    offers = resp.json().get("data", {}).get("offers", [])
    if not offers:
        return []

    def offer_price(o):
        return float(o["price"]["amount"])

    offers_sorted = sorted(offers, key=offer_price)

    seen_airlines = set()
    top = []
    for o in offers_sorted:
        airline_names = [a["name"] for a in o.get("airlines", [])]
        airline = " + ".join(airline_names) if airline_names else "Unknown"
        primary = airline_names[0] if airline_names else "Unknown"
        if primary in seen_airlines:
            continue
        seen_airlines.add(primary)
        top.append({
            "price": offer_price(o),
            "airline": airline,
            "verdict": None,  # Kiwi doesn't provide a low/typical/high verdict
        })
        if len(top) >= limit:
            break
    return top


def merge_top_fares(*fare_lists, limit=3):
    """Combines fares from multiple sources, keeps the cheapest entry per
    distinct airline label, and returns the overall top `limit` cheapest."""
    combined = [f for fares in fare_lists for f in fares]
    if not combined:
        return []
    combined.sort(key=lambda f: f["price"])
    seen_airlines = set()
    top = []
    for f in combined:
        if f["airline"] in seen_airlines:
            continue
        seen_airlines.add(f["airline"])
        top.append(f)
        if len(top) >= limit:
            break
    return top


def compute_signal(history, target, depart_date):
    """history: list of {"date": ..., "fares": [{"price", "airline", "verdict"}, ...]}"""
    if not history:
        return "watch", "No price history yet."

    day_cheapest = [min(day["fares"], key=lambda f: f["price"]) for day in history]
    prices = [f["price"] for f in day_cheapest]
    latest = prices[-1]
    latest_verdict = day_cheapest[-1]["verdict"]
    lowest = min(prices)
    prev = prices[-2] if len(prices) >= 2 else None
    days_to_departure = (date.fromisoformat(depart_date) - date.today()).days

    if target and latest <= target:
        return "buy", f"Cheapest fare today ${latest:.0f} is at or under your target ${target:.0f}."

    if latest_verdict == "low":
        return "buy", f"Google's own price insights call ${latest:.0f} \"low\" for this route/period."

    if len(history) >= 3 and latest <= lowest * 1.03 and 0 <= days_to_departure <= 45:
        return (
            "buy",
            f"Cheapest fare today ${latest:.0f} is near the lowest logged (${lowest:.0f}), "
            "within the typical 6-8 week booking window.",
        )

    if 0 <= days_to_departure <= 14:
        return "buy", "Inside two weeks of departure — fares rarely fall further this close in."

    if prev is not None and latest > prev and 0 <= days_to_departure <= 60:
        return "rising", f"Cheapest fare rose from ${prev:.0f} to ${latest:.0f} — worth deciding soon."

    if latest_verdict == "high":
        return "watch", f"Google flags ${latest:.0f} as \"high\" for this route right now — worth waiting."

    return "watch", f"No strong signal yet. Lowest seen so far: ${lowest:.0f}."


def send_email(to_addr, subject, body):
    if not (SMTP_USER and SMTP_PASS):
        print("SMTP not configured — skipping email. Message would have been:\n", body)
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = to_addr
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)


def main():
    cfg = load_json(CONFIG_PATH, {})
    if not cfg:
        raise SystemExit(f"Missing or empty config at {CONFIG_PATH}")

    history = load_json(HISTORY_PATH, [])
    state = load_json(STATE_PATH, {
        "last_digest_date": None,
        "last_buy_alert_date": None,
        "last_kiwi_date": None,
    })

    if not RAPIDAPI_KEY:
        raise SystemExit("Set the RAPIDAPI_KEY environment variable.")

    today = date.today().isoformat()

    google_fares = search_google_fares(cfg, limit=3)

    kiwi_fares = []
    if state.get("last_kiwi_date") != today:
        # Kiwi's free tier is capped at 200 requests/month — only call it
        # once per day (~30/month) rather than every 4-hour run.
        kiwi_fares = search_kiwi_fares(cfg, limit=3)
        state["last_kiwi_date"] = today

    fares = merge_top_fares(google_fares, kiwi_fares, limit=3)

    if not fares:
        print("No fares returned for this search — route/dates may need adjusting.")
        save_json(STATE_PATH, state)
        return

    history.append({"date": today, "fares": fares})
    save_json(HISTORY_PATH, history)

    cheapest = min(fares, key=lambda f: f["price"])
    signal, message = compute_signal(history, cfg.get("target_price_aud"), cfg["depart_date"])

    fares_line = ", ".join(f"{f['airline']} ${f['price']:.0f}" for f in fares)
    print(f"[{today}] {cfg['origin']}->{cfg['destination']}: {fares_line} ({signal}) - {message}")

    fares_list = "\n".join(f"  - {f['airline']}: ${f['price']:.0f}" for f in fares)

    # Once-a-day digest: top 3 fares + current signal, regardless of signal.
    if state.get("last_digest_date") != today:
        subject = f"[Fare Watch] {cfg['origin']}-{cfg['destination']} daily check — {signal.upper()} — ${cheapest['price']:.0f} AUD"
        body = f"{message}\n\nToday's cheapest options:\n{fares_list}"
        send_email(cfg["email_to"], subject, body)
        state["last_digest_date"] = today

    # Extra same-day nudge if a buy signal shows up after the digest already
    # went out this morning — capped at one extra per day.
    elif signal == "buy" and state.get("last_buy_alert_date") != today:
        subject = f"[Fare Watch] {cfg['origin']}-{cfg['destination']}: BUY NOW — ${cheapest['price']:.0f} AUD"
        body = f"{message}\n\nToday's cheapest options:\n{fares_list}"
        send_email(cfg["email_to"], subject, body)
        state["last_buy_alert_date"] = today

    # Always persist state at the end — this is what makes last_kiwi_date
    # actually stick even on runs where neither email branch fires.
    save_json(STATE_PATH, state)


if __name__ == "__main__":
    main()
