#!/usr/bin/env python3
import os
import requests
from datetime import datetime, timezone
import traceback

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
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("DEBUG: Telegram token or chat ID missing, skipping send.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        response = requests.post(url, data=payload)
        response.raise_for_status()
    except Exception as e:
        print(f"ERROR: Failed to send Telegram message: {e}")

# === DATA FETCH ===
def get_fpl_data():
    return requests.get(FPL_API_URL).json()

def get_fixtures():
    return requests.get(FIXTURES_URL).json()

def get_next_deadline(events):
    for event in events:
        if not event.get("finished", False):
            try:
                deadline = datetime.fromisoformat(event["deadline_time"].replace("Z", "+00:00"))
            except Exception:
                continue
            return event.get("name","Unknown GW"), event.get("id"), deadline
    return None, None, None

def build_team_map(teams):
    return {t["id"]: t.get("short_name", t.get("name", str(t["id"]))) for t in teams}

# === FIXTURE DIFFICULTY ===
def calculate_fixture_difficulty(fixtures, teams):
    team_fixtures = {t["id"]: [] for t in teams}
    for f in fixtures:
        if not f.get("finished", False):
            team_fixtures.setdefault(f.get("team_h"), []).append(f.get("team_h_difficulty", 3))
            team_fixtures.setdefault(f.get("team_a"), []).append(f.get("team_a_difficulty", 3))
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
        top_in = sorted(players, key=lambda x: x.get("transfers_in_event",0), reverse=True)[:5]
        top_out = sorted(players, key=lambda x: x.get("transfers_out_event",0), reverse=True)[:5]
        top_value = sorted(players, key=lambda x: x.get("points_per_cost",0), reverse=True)[:10]
        top_form = sorted(players, key=lambda x: x.get("form_value",0), reverse=True)[:5]
        diffs = [p for p in players if float(p.get("selected_by_percent", 0)) < 10]
        top_diff = sorted(diffs, key=lambda x: x.get("total_points",0), reverse=True)[:5]
        top_form_fixture = sorted(players, key=lambda x: x.get("form_fixture_score",0), reverse=True)[:5]

        def fmt(title, data, metric, round_digits=2):
            if not data: return f"*{title}:*\nNone"
            lines = [f"{i+1}. {p['web_name']} ({round(p.get(metric,0), round_digits)})" for i, p in enumerate(data)]
            return f"*{title}:*\n" + "\n".join(lines)

        section = (
            f"\n\n*{pos_name}*\n"
            f"{fmt('Top 5 In', top_in, 'transfers_in_event', 0)}\n\n"
            f"{fmt('Top 5 Out', top_out, 'transfers_out_event', 0)}\n\n"
            f"{fmt('Top 10 by Value', top_value, 'points_per_cost', 2)}\n\n"
            f"{fmt('Top 5 Form', top_form, 'form_value', 2)}\n\n"
            f"{fmt('Top 5 Differentials (<10%)', top_diff, 'total_points', 0)}\n\n"
            f"{fmt('Top 5 Form+Fixture', top_form_fixture, 'form_fixture_score', 2)}"
        )
        output.append(section)
    return "\n".join(output)

def build_watchlist(elements, teams_map):
    lines = []
    for pos_id, pos_name in POSITION_MAP.items():
        players = [p for p in elements if p["element_type"] == pos_id]
        for p in players:
            p["watch_score"] = (p.get("form_fixture_score",0) * 2) + p.get("points_per_cost",0)
        top2 = sorted(players, key=lambda x: x["watch_score"], reverse=True)[:2]
        if top2:
            section = "\n".join([
                f"{i+1}. {p['web_name']} ({teams_map.get(p['team'],'?')}) — score {round(p['watch_score'],1)}"
                for i,p in enumerate(top2)
            ])
        else:
            section = "None"
        lines.append(f"*{pos_name}:*\n{section}")
    return "\n\n".join(lines)

def get_team_summary(team_id):
    if not team_id: return ""
    try:
        data = requests.get(f"https://fantasy.premierleague.com/api/entry/{team_id}/").json()
        name = data.get("name","")
        rank = data.get("summary_overall_rank","N/A")
        points = data.get("summary_overall_points","N/A")
        return f"\n\n*Your Team: {name}*\nRank: {rank}\nPoints: {points}\n──────────────────────"
    except Exception as e:
        print(f"ERROR: Failed to fetch team summary: {e}")
        return "\n\n*Could not fetch your team summary.*\n──────────────────────"

# === MAIN DAILY DIGEST ===
def run():
    print("DEBUG: Script started")
    try:
        data = get_fpl_data()
        fixtures = get_fixtures()
        gw_name, _, deadline = get_next_deadline(data.get("events", []))

        print(f"Next Gameweek: {gw_name}, deadline: {deadline}")
        print(f"Telegram Chat ID: {CHAT_ID}, Token present: {TELEGRAM_TOKEN is not None}")

        if not deadline:
            print("DEBUG: No upcoming deadline found, exiting.")
            return

        teams = data.get("teams", [])
        team_fdr = calculate_fixture_difficulty(fixtures, teams)
        teams_map = build_team_map(teams)
        elements = enrich_players(data.get("elements", []), team_fdr)

        # Daily digest at configured hour only
        now = datetime.now(timezone.utc)
        if now.hour == DAILY_DIGEST_HOUR:
            watchlist = build_watchlist(elements, teams_map)
            summary = summarize_players(elements)
            header = (
                f"⚽ *Daily FPL Digest: {gw_name}*\n"
                f"🗓 Deadline: {deadline.astimezone().strftime('%a %d %b %H:%M')}\n"
                f"──────────────────────"
            )
            team_summary = get_team_summary(FPL_TEAM_ID) if FPL_TEAM_ID else ""
            message = f"{header}\n\n*Watchlist*\n{watchlist}\n{team_summary}\n{summary}"

            # Telegram message split
            for i in range(0, len(message), 3900):
                send_telegram_message(message[i:i+3900])
            print("DEBUG: Digest sent successfully.")

    except Exception as e:
        print(f"ERROR: Script failed: {e}")
        traceback.print_exc()


if __name__ == "__main__":
    run()
