#!/usr/bin/env python3
import os
import requests
from datetime import datetime, timezone
import sys
import re # For cleaning player names

# === CONFIG ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
# === ACTION REQUIRED: REPLACE 'None' WITH YOUR FPL TEAM ID (e.g., '123456') ===
FPL_TEAM_ID = os.getenv("FPL_TEAM_ID", None)  # Set your FPL ID here if not using env vars: FPL_TEAM_ID = '123456'

FPL_API_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
FIXTURES_URL = "https://fantasy.premierleague.com/api/fixtures/"
POSITION_MAP = {1: "Goalkeepers", 2: "Defenders", 3: "Midfielders", 4: "Forwards"}

# === UTILITIES ===
def clean_and_limit_text(text, limit=4096):
    """Ensure message length is within Telegram limit."""
    if len(text) > limit:
        # Simple truncation, better than crashing
        print(f"Warning: Message was truncated from {len(text)} to {limit} characters.")
        return text[:limit]
    return text

# === TELEGRAM SEND ===
def send_telegram_message(text):
    text = clean_and_limit_text(text)
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
        if not event.get("finished", True): 
            try:
                deadline_str = event.get("deadline_time", "").replace("Z", "+00:00")
                if not deadline_str:
                    continue
                deadline = datetime.fromisoformat(deadline_str)
                return event.get("name", "Unknown GW"), event.get("id"), deadline
            except (ValueError, KeyError) as e:
                print(f"Warning: Could not parse deadline time for event {event.get('id')}: {e}")
                continue
    return None, None, None

def get_my_team_picks(team_id):
    """Fetches the current 15 player element IDs in the user's squad."""
    if not team_id:
        return None
    try:
        url = f"https://fantasy.premierleague.com/api/my-team/{team_id}/"
        data = safe_fetch_json(url)
        
        # Check 1: Did the fetch completely fail?
        if not data:
            print(f"DEBUG: FAILED to get squad data from {url}. FPL API may be temporarily down or slow.")
            return None
        
        # Check 2: Was the 'picks' key present and non-empty?
        if 'picks' not in data or not data['picks']:
            print(f"DEBUG: Successfully fetched team data for {team_id}, but 'picks' list was MISSING or empty. This may indicate a temporary FPL API issue or a blank squad.")
            return None
        
        # Return a set of element IDs for easy lookup
        return {pick['element'] for pick in data['picks']}
    except Exception as e:
        print(f"ERROR: Failed to fetch user team picks (full error): {e}")
        return None

# === DATA PROCESSING ===
def get_team_data_map(teams):
    """
    Returns id -> {short_name, attack_strength, defence_strength} mapping.
    Attack/Defence strength are averaged from home/away FPL strength metrics.
    """
    team_data = {}
    for t in teams:
        # FPL strength metrics are typically out of 500. We normalize them slightly.
        avg_attack = (t.get("strength_attack_home", 300) + t.get("strength_attack_away", 300)) / 200
        avg_defence = (t.get("strength_defence_home", 300) + t.get("strength_defence_away", 300)) / 200
        
        team_data[t["id"]] = {
            "short_name": t.get("short_name", t.get("name", str(t["id"]))),
            "attack_strength": avg_attack,
            "defence_strength": avg_defence
        }
    return team_data

def calculate_fixture_difficulty(fixtures, teams):
    # Build mapping team_id -> list of upcoming difficulties and return FDR score
    team_fixtures = {t["id"]: [] for t in teams}
    for f in fixtures:
        if not f.get("finished", False):
            team_fixtures.setdefault(f["team_h"], []).append(f.get("team_h_difficulty", 3))
            team_fixtures.setdefault(f["team_a"], []).append(f.get("team_a_difficulty", 3))
    
    team_fdr = {}
    for team_id, diffs in team_fixtures.items():
        upcoming = diffs[:3]
        avg_diff = round(sum(upcoming) / len(upcoming), 2) if upcoming else 3.0
        team_fdr[team_id] = avg_diff
    return team_fdr

def build_fixture_map(fixtures, teams_map_short, current_gw_id):
    """Returns a map of team_id -> list of next 3 fixture strings (Team Name (Difficulty))"""
    team_fixtures = {t_id: [] for t_id in teams_map_short.keys()}
    
    # Filter for fixtures in the next 3 gameweeks after the current one
    # Note: We filter by GW ID > current GW ID
    relevant_fixtures = sorted([f for f in fixtures if f.get("event") and f["event"] > current_gw_id and not f.get("finished", False)], key=lambda x: x["event"])

    for f in relevant_fixtures:
        # Determine opponent and difficulty for Home team
        opp_a = teams_map_short.get(f["team_a"], '?')
        diff_h = f.get("team_h_difficulty", 3)
        fixture_h_str = f"{opp_a}({diff_h})"
        
        # Determine opponent and difficulty for Away team
        opp_h = teams_map_short.get(f["team_h"], '?')
        diff_a = f.get("team_a_difficulty", 3)
        fixture_a_str = f"{opp_h}({diff_a})"

        # Append if less than 3 fixtures have been added
        if len(team_fixtures[f["team_h"]]) < 3:
            team_fixtures[f["team_h"]].append(fixture_h_str)
        if len(team_fixtures[f["team_a"]]) < 3:
            team_fixtures[f["team_a"]].append(fixture_a_str)

    # Convert the list of fixture strings into a single, comma-separated string
    # e.g., 'WHU(2), SOU(3), ARS(4)'
    return {team_id: ", ".join(fixtures) for team_id, fixtures in team_fixtures.items()}


def enrich_players(elements, team_fdr, team_fixture_map):
    for p in elements:
        cost = p.get("now_cost", 0)
        p["points_per_cost"] = p.get("total_points", 0) / (cost / 10) if cost else 0
        
        try:
            p["form_value"] = float(p.get("form") or 0.0)
        except Exception:
            p["form_value"] = 0.0

        team_id = p.get("team")
        p["fixture_difficulty"] = team_fdr.get(team_id, 3.0)
        p["form_fixture_score"] = p["form_value"] * (5.5 - p["fixture_difficulty"])
        # New: Add the next 3 fixture string
        p["next_fixtures"] = team_fixture_map.get(team_id, "N/A")

    return elements

def get_captaincy_picks(team_data_map, fixtures, current_gw_id):
    """
    Analyzes the next gameweek fixtures based on FPL's team strength metrics
    to suggest captaincy candidates for attacking and defensive returns.
    """
    next_gw_id = current_gw_id + 1
    next_fixtures = [f for f in fixtures if f.get("event") == next_gw_id]

    attacking_candidates = []
    defensive_candidates = []
    
    for f in next_fixtures:
        team_h_id = f["team_h"]
        team_a_id = f["team_a"]
        
        team_h = team_data_map.get(team_h_id)
        team_a = team_data_map.get(team_a_id)

        if not team_h or not team_a:
            continue

        # --- Attacking Potential ---
        # Attacking Score for Team H: H Attack Strength + (6 - A Defence Strength)
        score_h_att = team_h["attack_strength"] + (6 - team_a["defence_strength"])
        attacking_candidates.append({
            "team_id": team_h_id,
            "opponent_id": team_a_id,
            "score": score_h_att,
            "type": "Attacking",
            "venue": "(H)"
        })
        
        # Attacking Score for Team A: A Attack Strength + (6 - H Defence Strength)
        score_a_att = team_a["attack_strength"] + (6 - team_h["defence_strength"])
        attacking_candidates.append({
            "team_id": team_a_id,
            "opponent_id": team_h_id,
            "score": score_a_att,
            "type": "Attacking",
            "venue": "(A)"
        })

        # --- Defensive Potential ---
        # Defensive Score for Team H: H Defence Strength + (6 - A Attack Strength)
        score_h_def = team_h["defence_strength"] + (6 - team_a["attack_strength"])
        defensive_candidates.append({
            "team_id": team_h_id,
            "opponent_id": team_a_id,
            "score": score_h_def,
            "type": "Defensive",
            "venue": "(H)"
        })

        # Defensive Score for Team A: A Defence Strength + (6 - H Attack Strength)
        score_a_def = team_a["defence_strength"] + (6 - team_h["attack_strength"])
        defensive_candidates.append({
            "team_id": team_a_id,
            "opponent_id": team_h_id,
            "score": score_a_def,
            "type": "Defensive",
            "venue": "(A)"
        })


    # Sort and pick top 3
    top_attack = sorted(attacking_candidates, key=lambda x: x["score"], reverse=True)[:3]
    top_defence = sorted(defensive_candidates, key=lambda x: x["score"], reverse=True)[:3]

    lines = [f"*🎯 Captaincy & Transfer Targets (GW {next_gw_id})*\n"]
    
    lines.append("*Top 3 Attacking Fixtures (Goals/Assists potential):*")
    for i, c in enumerate(top_attack):
        team_name = team_data_map[c["team_id"]]["short_name"]
        opp_name = team_data_map[c["opponent_id"]]["short_name"]
        lines.append(f"{i+1}. {team_name} {c['venue']} vs {opp_name} (Score: {round(c['score'], 2)})")

    lines.append("\n*Top 3 Defensive Fixtures (Clean Sheet potential):*")
    for i, c in enumerate(top_defence):
        team_name = team_data_map[c["team_id"]]["short_name"]
        opp_name = team_data_map[c["opponent_id"]]["short_name"]
        lines.append(f"{i+1}. {team_name} {c['venue']} vs {opp_name} (Score: {round(c['score'], 2)})")
        
    return "\n".join(lines) + "\n══════════════════════"

def get_personal_analysis(my_picks, elements, teams_map_short):
    """
    Analyzes the user's current squad for captaincy and suggests potential transfers.
    """
    if not my_picks:
        # NOTE: This message is triggered when get_my_team_picks fails to return the squad list.
        return "*Personalized Analysis:* Could not retrieve your current FPL squad (API access issue). Try again later."

    my_squad = [p for p in elements if p.get("id") in my_picks]
    
    if not my_squad:
        return "*Personalized Analysis:* No players found matching your squad."

    analysis_lines = []
    
    # --- 1. Captaincy Suggestion ---
    
    # Sort my squad based on the pre-calculated 'form_fixture_score'
    captaincy_candidates = sorted(my_squad, key=lambda x: x.get("form_fixture_score", 0), reverse=True)
    
    captain_text = "*👑 Your Captaincy Suggestion (GW Next):*"
    if captaincy_candidates:
        best_candidate = captaincy_candidates[0]
        team_short = teams_map_short.get(best_candidate.get("team"), '?')
        score = round(best_candidate.get("form_fixture_score", 0), 2)
        
        captain_text += (
            f"\nYour best pick is *{best_candidate.get('web_name', 'N/A')}* ({team_short})."
            f"\n  - Metric Score (Form x Fixture): {score}"
            f"\n  - Next 3 Fixtures: {best_candidate.get('next_fixtures', 'N/A')}"
        )
    else:
        captain_text += "\nNo suitable captain candidates found in your squad."
    
    analysis_lines.append(captain_text)

    # --- 2. Transfer Suggestions ---

    # Get the top 3 watchlist players for each position
    watchlist_by_pos = {}
    for pos_id, pos_name in POSITION_MAP.items():
        players_in_pos = [p for p in elements if p.get("element_type") == pos_id]
        # Calculate watch score as in build_watchlist
        for p in players_in_pos:
            p["watch_score"] = (p.get("form_fixture_score", 0) * 2) + p.get("points_per_cost", 0)
        
        # Only look at players NOT already in the user's squad for buying suggestions
        top_watch = sorted([p for p in players_in_pos if p["id"] not in my_picks], 
                           key=lambda x: p.get("watch_score", 0), reverse=True)[:3]
        watchlist_by_pos[pos_id] = top_watch

    transfer_suggestions = []
    
    # Analyze squad for weakest players
    for pos_id, pos_name in POSITION_MAP.items():
        squad_pos = [p for p in my_squad if p.get("element_type") == pos_id]
        if not squad_pos:
            continue
            
        # Filter for players who have played this season (Total Points > 0)
        active_squad_pos = [p for p in squad_pos if p.get("total_points", 0) > 0]
        
        # Sort by weakest Form/Fixture score (lower score is worse)
        weakest_players = sorted(active_squad_pos, key=lambda x: x.get("form_fixture_score", 0))

        if weakest_players:
            player_to_sell = weakest_players[0]
            sell_score = player_to_sell.get("form_fixture_score", 0)
            
            # Compare against the top watchlist player in that position
            top_buy_candidates = watchlist_by_pos.get(pos_id, [])
            
            if top_buy_candidates:
                player_to_buy = top_buy_candidates[0]
                buy_score = player_to_buy.get("form_fixture_score", 0)
                
                # Suggest a transfer if the potential buy is significantly better (e.g., score difference > 1.0)
                if buy_score > sell_score + 1.0:
                    sell_team = teams_map_short.get(player_to_sell.get("team"), '?')
                    buy_team = teams_map_short.get(player_to_buy.get("team"), '?')
                    
                    sell_name = player_to_sell.get('web_name', 'N/A')
                    buy_name = player_to_buy.get('web_name', 'N/A')
                    
                    transfer_suggestions.append(
                        f"🔄 *{pos_name}:* Sell *{sell_name}* ({sell_team}, Score: {round(sell_score, 2)}) "
                        f"for *{buy_name}* ({buy_team}, Score: {round(buy_score, 2)})"
                    )

    transfer_text = "\n*🛒 Transfer Suggestions (Sell Weakest Player):*"
    transfer_text += "\n" + "\n".join(transfer_suggestions) if transfer_suggestions else "\nYour squad looks well-balanced for the upcoming fixtures! No strong transfer calls."

    analysis_lines.append(transfer_text)
    
    return "\n\n".join(analysis_lines) + "\n══════════════════════"

# The original get_player_health_status function is removed as requested.

def summarize_players(elements, teams_map_short):
    summaries = []
    
    # Enhanced formatting function
    def fmt(title, data, metric, round_digits=2, show_fixtures=False):
        # Use an improved separator for aesthetics
        section_title = f"\n\n\n*🔸 {title} 🔸*\n{'—' * 20}"
        if not data: return f"{section_title}\nNone"

        lines = []
        for i, p in enumerate(data):
            val = p.get(metric, 0)
            val_str = str(round(val, round_digits)) if isinstance(val, (float, int)) else str(val)
            
            team_short = teams_map_short.get(p.get("team"), '?')
            total_points = p.get('total_points', 0) # Fetched total points
            
            # Player line: Rank. Name (Team) (Metric Score) (Pts: Total Points) [Fixtures]
            # Updated line construction to include total points
            line = f"{i+1}. {p.get('web_name', 'N/A')} ({team_short}) ({val_str}) (Pts: {total_points})"
            
            if show_fixtures:
                line += f" (Next 3: {p.get('next_fixtures', 'N/A')})"
                
            lines.append(line)
            
        return f"{section_title}\n" + "\n".join(lines)

    for pos_id, pos_name in POSITION_MAP.items():
        players = [p for p in elements if p.get("element_type") == pos_id]
        
        # --- Data Sorting ---
        # Transfers In/Out: Keep round_digits=0
        top_in = sorted(players, key=lambda x: x.get("transfers_in_event", 0), reverse=True)[:5]
        top_out = sorted(players, key=lambda x: x.get("transfers_out_event", 0), reverse=True)[:5]
        
        # Top 10 Value: show_fixtures=True
        top_value = sorted(players, key=lambda x: x.get("points_per_cost", 0), reverse=True)[:10]

        # Top 5 Form: show_fixtures=True
        top_form = sorted(players, key=lambda x: x.get("form_value", 0), reverse=True)[:5]
        
        # Top 5 Differentials: Keep round_digits=0, show_fixtures=True
        diffs = [p for p in players if float(p.get("selected_by_percent", 0)) < 10]
        top_diff = sorted(diffs, key=lambda x: x.get("total_points", 0), reverse=True)[:5]

        # Top 5 Form + Fixture: show_fixtures=True
        top_fixture_form = sorted(players, key=lambda x: x.get("form_fixture_score", 0), reverse=True)[:5]
        
        # --- Section Assembly ---
        section = (
            f"\n\n\n⭐ *{pos_name} Analysis* ⭐"
            f"{fmt('Top 5 Transfers IN (This GW)', top_in, 'transfers_in_event', 0)}"
            f"{fmt('Top 5 Transfers OUT (This GW)', top_out, 'transfers_out_event', 0)}"
            f"{fmt('Top 10 Points/Cost Value', top_value, 'points_per_cost', 2, show_fixtures=True)}"
            f"{fmt('Top 5 Form Players', top_form, 'form_value', 2, show_fixtures=True)}"
            f"{fmt
