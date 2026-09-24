"""
Fare Watch — checks a round-trip fare, logs it, and emails you when it
looks like a good time to buy.

Data source: "Google Flights Live API" on RapidAPI (free BASIC tier).
Subscribe at https://rapidapi.com/mtnrabi/api/google-flights-live-api
to get an X-RapidAPI-Key.

NOTE on the endpoint: host, path, and headers below were confirmed
directly from a real RapidAPI playground request/response.

Run manually:
    RAPIDAPI_KEY=... python fare_watch.py

Or let the included GitHub Actions workflow run it on a schedule.
"""

import os
import json
import smtplib
import ssl
from email.mime.text import MIMEText
from datetime import date

import requests

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
HISTORY_PATH = os.path.join(os.path.dirname(__file__), "price_history.json")

RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY")
RAPIDAPI_HOST = "google-flights-live-api.p.rapidapi.com"
RAPIDAPI_ROUNDTRIP_PATH = "/api/google_flights/roundtrip/v1"  # confirmed via playground snippet
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


def search_top_fares(cfg, limit=3):
    """Returns a list of up to `limit` cheapest itineraries, each as
    {"price": float, "airline": str, "verdict": str}, sorted cheapest
    first. Empty list if nothing came back."""
    body = {
        "from_airport": cfg["origin"],
        "to_airport": cfg["destination"],
        "departure_date": cfg["depart_date"],
        "return_date": cfg["return_date"],
    }
    resp = requests.post(
        f"https://{RAPIDAPI_HOST}{RAPIDAPI_ROUNDTRIP_PATH}",
        headers={
            "X-RapidAPI-Key": RAPIDAPI_KEY,
            "X-RapidAPI-Host": RAPIDAPI_HOST,
            "Content-Type": "application/json",
        },
        json=body,
        timeout=30,
    )
    if resp.status_code == 404:
        raise SystemExit(
            f"Got a 404 from {RAPIDAPI_ROUNDTRIP_PATH} — check the RapidAPI playground "
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

    if not RAPIDAPI_KEY:
        raise SystemExit("Set the RAPIDAPI_KEY environment variable.")

    fares = search_top_fares(cfg, limit=3)

    if not fares:
        print("No fares returned for this search — route/dates may need adjusting.")
        return

    today = date.today().isoformat()
    history.append({"date": today, "fares": fares})
    save_json(HISTORY_PATH, history)

    cheapest = min(fares, key=lambda f: f["price"])
    signal, message = compute_signal(history, cfg.get("target_price_aud"), cfg["depart_date"])

    fares_line = ", ".join(f"{f['airline']} ${f['price']:.0f}" for f in fares)
    print(f"[{today}] {cfg['origin']}->{cfg['destination']}: {fares_line} ({signal}) - {message}")

    if signal in ("buy", "rising"):
        subject = f"[Fare Watch] {cfg['origin']}-{cfg['destination']}: {signal.upper()} — ${cheapest['price']:.0f} AUD"
        fares_list = "\n".join(f"  - {f['airline']}: ${f['price']:.0f}" for f in fares)
        body = f"{message}\n\nToday's cheapest options:\n{fares_list}"
        send_email(cfg["email_to"], subject, body)


if __name__ == "__main__":
    main()
