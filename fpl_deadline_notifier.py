#!/usr/bin/env python3
import os
import requests
from datetime import datetime, timezone
import sys # For graceful exit

# === CONFIG ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
FPL_TEAM_ID = os.getenv("FPL_TEAM_ID")  # optional

FPL_API_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
FIXTURES_URL = "https://fantasy.premierleague.com/api/fixtures/"
POSITION_MAP = {1: "Goalkeepers", 2: "Defenders", 3: "Midfielders", 4: "Forwards"}

# === TELEGRAM SEND ===
def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("DEBUG: Telegram token or chat ID missing, skipping send.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        response = requests.post(url, data=payload, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"ERROR: Failed to send Telegram message: {e}")

# === FPL FETCH (with robustness) ===
def safe_fetch_json(url):
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"ERROR: Failed to fetch data from {url}: {e}")
        return None

def get_fpl_data():
    return safe_fetch_json(FPL_API_URL)

def get_fixtures():
    return safe_fetch_json(FIXTURES_URL)

def get_next_deadline(events):
    for event in events:
        # Use .get() to safely check if event is finished
        if not event.get("finished", True): 
            try:
                # Replace 'Z' with '+00:00' for proper ISO 8601 parsing in all Python versions
                deadline_str = event.get("deadline_time", "").replace("Z", "+00:00")
                if not deadline_str:
                    continue
                deadline = datetime.fromisoformat(deadline_str)
                return event.get("name", "Unknown GW"), event.get("id"), deadline
            except (ValueError, KeyError) as e:
                print(f"Warning: Could not parse deadline time for event {event.get('id')}: {e}")
                continue
    return None, None, None

# === DATA PROCESSING ===
def build_team_map(teams):
    return {t["id"]: t.get("short_name", t.get("name", str(t["id"]))) for t in teams}

def calculate_fixture_difficulty(fixtures, teams):
    team_fixtures = {t["id"]: [] for t in teams}
    for f in fixtures:
        if not f.get("finished", False):
            # We don't need to check for existence of keys here because we used safe_fetch_json
            team_fixtures.setdefault(f["team_h"], []).append(f.get("team_h_difficulty", 3))
            team_fixtures.setdefault(f["team_a"], []).append(f.get("team_a_difficulty", 3))
    
    team_fdr = {}
    for team_id, diffs in team_fixtures.items():
        # Get average of next 3 fixtures
        upcoming = diffs[:3]
        avg_diff = round(sum(upcoming) / len(upcoming), 2) if upcoming else 3.0
        team_fdr[team_id] = avg_diff
    return team_fdr

def enrich_players(elements, team_fdr):
    for p in elements:
        cost = p.get("now_cost", 0)
        p["points_per_cost"] = p.get("total_points", 0) / (cost / 10) if cost else 0
        
        try:
            p["form_value"] = float(p.get("form") or 0.0)
        except Exception:
            p["form_value"] = 0.0

        p["fixture_difficulty"] = team_fdr.get(p.get("team"), 3.0)
        # Combined: form * (5.5 - fdr) — easier fixtures (lower fdr) boost score
        p["form_fixture_score"] = p["form_value"] * (5.5 - p["fixture_difficulty"])
    return elements

def summarize_players(elements):
    summaries = []
    
    for pos_id, pos_name in POSITION_MAP.items():
        players = [p for p in elements if p.get("element_type") == pos_id]

        top_in = sorted(players, key=lambda x: x.get("transfers_in_event", 0), reverse=True)[:5]
        top_out = sorted(players, key=lambda x: x.get("transfers_out_event", 0), reverse=True)[:5]
        top_value = sorted(players, key=lambda x: x.get("points_per_cost", 0), reverse=True)[:10]
        top_form = sorted(players, key=lambda x: x.get("form_value", 0), reverse=True)[:5]
        
        diffs = [p for p in players if float(p.get("selected_by_percent", 0)) < 10]
        top_diff = sorted(diffs, key=lambda x: x.get("total_points", 0), reverse=True)[:5]

        top_fixture_form = sorted(players, key=lambda x: x.get("form_fixture_score", 0), reverse=True)[:5]

        def fmt(title, data, metric, round_digits=2):
            if not data: return f"*{title}:*\nNone"
            lines = []
            for i, p in enumerate(data):
                val = p.get(metric, 0)
                val_str = str(round(val, round_digits)) if isinstance(val, (float, int)) else str(val)
                lines.append(f"{i+1}. {p.get('web_name', 'N/A')} ({val_str})")
            return f"*{title}:*\n" + "\n".join(lines)

        section = (
            f"\n\n*{pos_name}*\n"
            f"{fmt('Top 5 In', top_in, 'transfers_in_event', 0)}\n\n"
            f"{fmt('Top 5 Out', top_out, 'transfers_out_event', 0)}\n\n"
            f"{fmt('Top 10 by Points/Cost', top_value, 'points_per_cost', 2)}\n\n"
            f"{fmt('Top 5 Form', top_form, 'form_value', 2)}\n\n"
            f"{fmt('Top 5 Differentials (<10%)', top_diff, 'total_points', 0)}\n\n"
            f"{fmt('Top 5 Form + Fixture', top_fixture_form, 'form_fixture_score', 2)}"
        )
        summaries.append(section)

    return "\n".join(summaries)

def build_watchlist(elements, teams_map):
    watch_sections = []
    for pos_id, pos_name in POSITION_MAP.items():
        players = [p for p in elements if p.get("element_type") == pos_id]
        
        for p in players:
            # watch_score = (form_fixture_score * 2) + points_per_cost
            p["watch_score"] = (p.get("form_fixture_score", 0) * 2) + p.get("points_per_cost", 0)

        top_watch = sorted(players, key=lambda x: x.get("watch_score", 0), reverse=True)[:2]
        lines = []
        for i, p in enumerate(top_watch):
            team_short = teams_map.get(p.get("team"), str(p.get("team")))
            price = (p.get("now_cost", 0) / 10)
            form = round(p.get("form_value", 0), 2)
            fdr = p.get("fixture_difficulty", 3.0)
            pts = p.get("total_points", 0)
            owned = p.get("selected_by_percent", "0")
            ws = round(p.get("watch_score", 0), 2)
            lines.append(f"{i+1}. {p.get('web_name', 'N/A')} — {team_short} — £{price} — pts:{pts} — form:{form} — fdr:{fdr} — own:{owned}% — score:{ws}")
        
        watch_text = "\n".join(lines) if lines else "None"
        watch_sections.append(f"*{pos_name}:*\n{watch_text}")
    return "\n\n".join(watch_sections)

def get_team_summary(team_id):
    if not team_id:
        return ""
    try:
        url = f"https://fantasy.premierleague.com/api/entry/{team_id}/"
        data = safe_fetch_json(url)
        if not data:
            raise Exception("No team data received.")

        name = data.get("name", "")
        player_name = f"{data.get('player_first_name', '')} {data.get('player_last_name', '')}".strip()
        rank = data.get("summary_overall_rank", "N/A")
        points = data.get("summary_overall_points", "N/A")
        transfers = data.get("last_deadline_total_transfers", "N/A")
        
        return (
            f"\n\n*Your Team: {name} ({player_name})*\n"
            f"Rank: {rank}\n"
            f"Points: {points}\n"
            f"Transfers Made (last GW): {transfers}\n"
            "──────────────────────"
        )
    except Exception:
        return "\n\n*Could not fetch your team summary.*\n──────────────────────"

# === MAIN DIGEST ===
def run_daily_digest():
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("ERROR: TELEGRAM_TOKEN or CHAT_ID is not set. Cannot send digest.")
        # Exit gracefully, but with an error code if critical vars are missing
        sys.exit(1)

    data = get_fpl_data()
    fixtures = get_fixtures()

    if not data or not fixtures:
        print("ERROR: Essential FPL data not available. Exiting.")
        return

    gw_name, gw_id, deadline = get_next_deadline(data.get("events", []))
    if not deadline:
        print("INFO: Could not find next GW deadline. Perhaps the season is over?")
        return

    teams = data.get("teams", [])
    teams_map = build_team_map(teams)
    team_fdr = calculate_fixture_difficulty(fixtures, teams)
    elements = enrich_players(data.get("elements", []), team_fdr)

    # Build digest components
    watchlist_text = build_watchlist(elements, teams_map)
    team_summary = get_team_summary(FPL_TEAM_ID)
    stats = summarize_players(elements)

    # Time calculations
    now = datetime.now(timezone.utc)
    diff = deadline - now
    days = diff.days
    hours = (diff.seconds // 3600)

    header = (
        f"⚽ *Daily FPL Digest: {gw_name}*\n"
        f"🕒 Deadline in {days}d {hours}h\n"
        f"🗓 {deadline.astimezone().strftime('%a %d %b %H:%M')}\n"
        f"──────────────────────"
    )

    message = f"{header}\n\n*Recommended Watchlist (Top 2 by Position)*\n{watchlist_text}\n\n{team_summary}\n{stats}"

    # Telegram limit ~4096 chars — chunk
    for i in range(0, len(message), 3900):
        send_telegram_message(message[i:i+3900])

if __name__ == "__main__":
    run_daily_digest()
