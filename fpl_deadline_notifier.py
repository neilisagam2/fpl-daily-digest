#!/usr/bin/env python3
import os
import requests
from datetime import datetime, timezone

# === CONFIG ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
FPL_TEAM_ID = os.getenv("FPL_TEAM_ID")  # optional

FPL_API_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
FIXTURES_URL = "https://fantasy.premierleague.com/api/fixtures/"
POSITION_MAP = {1: "Goalkeepers", 2: "Defenders", 3: "Midfielders", 4: "Forwards"}

# Run daily digest at this hour (UTC)
DAILY_DIGEST_HOUR = 8

# === TELEGRAM ===
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    requests.post(url, data=payload)

# === DATA FETCH ===
def get_fpl_data():
    return requests.get(FPL_API_URL).json()

def get_fixtures():
    return requests.get(FIXTURES_URL).json()

def get_next_deadline(events):
    for event in events:
        if not event["finished"]:
            deadline = datetime.fromisoformat(event["deadline_time"].replace("Z", "+00:00"))
            return event["name"], event["id"], deadline
    return None, None, None

def build_team_map(teams):
    return {t["id"]: t.get("short_name", t.get("name", str(t["id"]))) for t in teams}

# === FIXTURE DIFFICULTY ===
def calculate_fixture_difficulty(fixtures, teams):
    team_fixtures = {t["id"]: [] for t in teams}_
