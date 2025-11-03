#!/usr/bin/env python3
import os
import requests
from datetime import datetime, timezone

# === CONFIG ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
FPL_API_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
FIXTURES_URL = "https://fantasy.premierleague.com/api/fixtures/"
FPL_TEAM_ID = os.getenv("FPL_TEAM_ID")  # optional

POSITION_MAP = {
    1: "Goalkeepers",
    2: "Defenders",
    3: "Midfielders",
    4: "Forwards"
}

# === TELEGRAM SEND ===
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    requests.post(url, data=payload)

# === FPL FETCH ===
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

# === FIXTURE DIFFICULTY ===
def calculate_fixture_difficulty(fixtures, teams):
    # Build mapping team_id -> list of upcoming difficulties
    team_fixtures = {t["id"]: [] for t in teams}
    now = datetime.now(timezone.utc)
    for f in fixtures:
        # we take upcoming fixtures only
        if not f.get("finished", False):
            # store difficulty in order (API returns fixtures sorted roughly by kickoff)
            team_fixtures.setdefault(f["team_h"], []).append(f.get("team_h_difficulty", 3))
            team_fixtures.setdefault(f["team_a"], []).append(f.get("team_a_difficulty", 3))

    # For each team compute avg of next 3 fixtures (or available ones)
    team_fdr = {}
    for team_id, diffs in team_fixtures.items():
        if diffs:
            upcoming = diffs[:3]
            avg_diff = round(sum(upcoming) / len(upcoming), 2)
        else:
            avg_diff = 3.0
        team_fdr[team_id] = avg_diff
    return team_fdr

# === TEAM NAME MAP ===
def build_team_map(teams):
    # Returns id -> short_name mapping
    return {t["id"]: t.get("short_name", t.get("name", str(t["id"]))) for t in teams}

# === PLAYER ANALYTICS ===
def enrich_players(elements, team_fdr):
    # Add derived metrics to each player
    for p in elements:
        # points per cost (now_cost is in tenths)
        now_cost = p.get("now_cost", 0)
        p["points_per_cost"] = p["total_points"] / (now_cost / 10) if now_cost else 0

        # form numeric
        try:
            p["form_value"] = float(p.get("form") or 0.0)
        except Exception:
            p["form_value"] = 0.0

        # fixture difficulty for the player's team
        p["fixture_difficulty"] = team_fdr.get(p.get("team"), 3.0)

        # combined: form * (5.5 - fdr) — easier fixtures (lower fdr) boost score
        p["form_fixture_score"] = p["form_value"] * (5.5 - p["fixture_difficulty"])

    return elements

def summarize_players(elements):
    summaries = []

    for pos_id, pos_name in POSITION_MAP.items():
        players = [p for p in elements if p["element_type"] == pos_id]

        # Top 5 Transfers In / Out
        top_in = sorted(players, key=lambda x: x.get("transfers_in_event", 0), reverse=True)[:5]
        top_out = sorted(players, key=lambda x: x.get("transfers_out_event", 0), reverse=True)[:5]

        # Top 10 Value
        top_value = sorted(players, key=lambda x: x.get("points_per_cost", 0), reverse=True)[:10]

        # Top 5 Form
        top_form = sorted(players, key=lambda x: x.get("form_value", 0), reverse=True)[:5]

        # Differentials (<10% ownership)
        diffs = [p for p in players if p.get("selected_by_percent")]
        diffs = [p for p in diffs if float(p["selected_by_percent"]) < 10.0]
        top_diff = sorted(diffs, key=lambda x: x.get("total_points", 0), reverse=True)[:5]

        # Top 5 Form + Fixture
        top_fixture_form = sorted(players, key=lambda x: x.get("form_fixture_score", 0), reverse=True)[:5]

        def fmt(title, data, metric, round_digits=2):
            if not data:
                return f"*{title}:*\nNone"
            lines = []
            for i, p in enumerate(data):
                val = p.get(metric, 0)
                if isinstance(val, float):
                    val_str = str(round(val, round_digits))
                else:
                    val_str = str(val)
                lines.append(f"{i+1}. {p['web_name']} ({val_str})")
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

# === WATCHLIST ===
def build_watchlist(elements, teams_map):
    # For each position choose top 2 players by composite watch_score
    # watch_score = (form_fixture_score * 2) + points_per_cost
    watch_sections = []
    for pos_id, pos_name in POSITION_MAP.items():
        players = [p for p in elements if p["element_type"] == pos_id]
        for p in players:
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
            lines.append(f"{i+1}. {p['web_name']} — {team_short} — £{price} — pts:{pts} — form:{form} — fdr:{fdr} — own:{owned}% — score:{ws}")
        if not lines:
            watch_text = "None"
        else:
            watch_text = "\n".join(lines)
        watch_sections.append(f"*{pos_name}:*\n{watch_text}")
    return "\n\n".join(watch_sections)

# === TEAM SUMMARY ===
def get_team_summary(team_id):
    if not team_id:
        return ""
    try:
        url = f"https://fantasy.premierleague.com/api/entry/{team_id}/"
        data = requests.get(url).json()
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
    data = get_fpl_data()
    fixtures = get_fixtures()
    gw_name, gw_id, deadline = get_next_deadline(data["events"])
    if not deadline:
        return

    teams = data.get("teams", [])
    teams_map = build_team_map(teams)

    team_fdr = calculate_fixture_difficulty(fixtures, teams)
    elements = enrich_players(data.get("elements", []), team_fdr)

    # Build watchlist (TL;DR)
    watchlist_text = build_watchlist(elements, teams_map)

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

    team_summary = get_team_summary(FPL_TEAM_ID)
    stats = summarize_players(elements)

    message = f"{header}\n\n*Recommended Watchlist (TL;DR)*\n{watchlist_text}\n\n{team_summary}\n{stats}"

    # Telegram limit ~4096 chars — chunk
    for i in range(0, len(message), 3900):
        send_telegram_message(message[i:i+3900])

if __name__ == "__main__":
    run_daily_digest()
