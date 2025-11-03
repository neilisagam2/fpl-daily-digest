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
    team_fixtures = {t["id"]: [] for t in teams}
    for f in fixtures:
        if not f.get("finished", False):
            team_fixtures.setdefault(f["team_h"], []).append(f.get("team_h_difficulty", 3))
            team_fixtures.setdefault(f["team_a"], []).append(f.get("team_a_difficulty", 3))
    team_fdr = {}
    for team_id, diffs in team_fixtures.items():
        avg_diff = round(sum(diffs[:3]) / len(diffs[:3]), 2) if diffs else 3.0
        team_fdr[team_id] = avg_diff
    return team_fdr

# === PLAYER ENRICH ===
def enrich_players(elements, team_fdr):
    for p in elements:
        cost = p.get("now_cost", 0)
        p["points_per_cost"] = p["total_points"] / (cost / 10) if cost else 0
        p["form_value"] = float(p.get("form") or 0.0)
        p["fixture_difficulty"] = team_fdr.get(p.get("team"), 3.0)
        p["form_fixture_score"] = p["form_value"] * (5.5 - p["fixture_difficulty"])
    return elements

# === SUMMARIES ===
def summarize_players(elements):
    output = []
    for pos_id, pos_name in POSITION_MAP.items():
        players = [p for p in elements if p["element_type"] == pos_id]
        top_in = sorted(players, key=lambda x: x["transfers_in_event"], reverse=True)[:5]
        top_out = sorted(players, key=lambda x: x["transfers_out_event"], reverse=True)[:5]
        top_value = sorted(players, key=lambda x: x["points_per_cost"], reverse=True)[:10]
        top_form = sorted(players, key=lambda x: x["form_value"], reverse=True)[:5]
        diffs = [p for p in players if float(p.get("selected_by_percent", 0)) < 10]
        top_diff = sorted(diffs, key=lambda x: x["total_points"], reverse=True)[:5]
        top_form_fixture = sorted(players, key=lambda x: x["form_fixture_score"], reverse=True)[:5]

        def fmt(title, data, metric, round_digits=2):
            if not data: return f"*{title}:*\nNone"
            lines = [f"{i+1}. {p['web_name']} ({round(p.get(metric,0), round_digits)})" for i, p in enumerate(data)]
            return f"*{title}:*\n" + "\n".join(lines)

        section = (
            f"\n\n*{pos_name}*\n"
            f"{fmt('Top 5 In', top_in, 'transfers_in_event', 0)}\n\n"
