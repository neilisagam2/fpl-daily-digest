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
FPL_TEAM_ID = '124780'  # <-- REPLACE 'None' with your numerical FPL Team ID (e.g., FPL_TEAM_ID = '123456')

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
        if not data or 'picks' not in data:
            return None
        
        # Return a set of element IDs for easy lookup
        return {pick['element'] for pick in data['picks']}
    except Exception as e:
        print(f"ERROR: Failed to fetch user team picks: {e}")
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
    
    # FIX: Filter for all UNPLAYED fixtures, starting from the current Gameweek onwards.
    # The previous logic incorrectly filtered out the current gameweek's fixtures.
    relevant_fixtures = sorted([f for f in fixtures if not f.get("finished", False) and f.get("event")], key=lambda x: x["event"])

    for f in relevant_fixtures:
        # Determine opponent and difficulty for Home team
        opp_a = teams_map_short.get(f["team_a"], '?')
        diff_h = f.get("team_h_difficulty", 3)
        fixture_h_str = f"{opp_a}({diff_h})"
        
        # Determine opponent and difficulty for Away team
        opp_h = teams_map_short.get(f["team_h"], '?')
        diff_a = f.get("team_a_difficulty", 3)
        fixture_a_str = f"{opp_h}({diff_a})"

        # Append only if less than 3 fixtures have been added for that team
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
    # NOTE: Since get_next_deadline returns the ID of the UPCOMING GW, we use that ID.
    next_fixtures = [f for f in fixtures if f.get("event") == current_gw_id] 

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

    lines = [f"*🎯 Captaincy & Transfer Targets (GW {current_gw_id})*\n"]
    
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
        return "*Personalized Analysis:* FPL Team ID not set or team data unavailable."

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
        # Fixed the string concatenation issue here by using f-string continuation (less prone to error)
        section = (
            f"\n\n\n⭐ *{pos_name} Analysis* ⭐"
            f"{fmt('Top 5 Transfers IN (This GW)', top_in, 'transfers_in_event', 0)}"
            f"{fmt('Top 5 Transfers OUT (This GW)', top_out, 'transfers_out_event', 0)}"
            f"{fmt('Top 10 Points/Cost Value', top_value, 'points_per_cost', 2, show_fixtures=True)}"
            f"{fmt('Top 5 Form Players', top_form, 'form_value', 2, show_fixtures=True)}"
            f"{fmt('Top 5 Differentials (<10% Ownership)', top_diff, 'total_points', 0, show_fixtures=True)}"
            f"{fmt('Top 5 Form + Fixture Rating', top_fixture_form, 'form_fixture_score', 2, show_fixtures=True)}"
        )
        summaries.append(section)

    return summaries # Return the list of sections instead of a single string


def build_watchlist(elements, teams_map_short):
    # watch_score = (form_fixture_score * 2) + points_per_cost
    watch_sections = []
    
    for pos_id, pos_name in POSITION_MAP.items():
        players = [p for p in elements if p.get("element_type") == pos_id]
        
        for p in players:
            p["watch_score"] = (p.get("form_fixture_score", 0) * 2) + p.get("points_per_cost", 0)

        top_watch = sorted(players, key=lambda x: x.get("watch_score", 0), reverse=True)[:3] # Changed to top 3 for more options
        
        lines = []
        for i, p in enumerate(top_watch):
            team_short = teams_map_short.get(p.get("team"), '?')
            price = (p.get("now_cost", 0) / 10)
            form = round(p.get("form_value", 0), 2)
            fdr = p.get("fixture_difficulty", 3.0)
            owned = p.get("selected_by_percent", "0")
            
            # Combine stats into a more compact line
            lines.append(
                f"{i+1}. *{p.get('web_name', 'N/A')}* ({team_short}) — £{price} "
                f"(Own:{owned}% | Form:{form} | FDR:{fdr})"
            )
            # Add fixtures on a separate, indented line for readability
            lines.append(f"   > Next 3: {p.get('next_fixtures', 'N/A')}")

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

        name = data.get("name", "N/A")
        player_name = f"{data.get('player_first_name', '')} {data.get('player_last_name', '')}".strip()
        rank = data.get("summary_overall_rank", "N/A")
        points = data.get("summary_overall_points", "N/A")
        transfers = data.get("last_deadline_total_transfers", "N/A")
        
        return (
            f"\n\n*👤 Your Team: {name} ({player_name})*\n"
            f"📈 *Overall Rank:* {rank}\n"
            f"📊 *Total Points:* {points}\n"
            f"🔄 *Last GW Transfers:* {transfers}\n"
            "══════════════════════"
        )
    except Exception:
        return "\n\n*Could not fetch your team summary.*\n══════════════════════"


# === MAIN DIGEST ===
def run_daily_digest():
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("ERROR: TELEGRAM_TOKEN or CHAT_ID is not set. Cannot send digest.")
        sys.exit(1)

    # Convert FPL_TEAM_ID to string if it was set directly to an integer
    fpl_team_id_str = str(FPL_TEAM_ID) if FPL_TEAM_ID is not None else None

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
    # NEW: Get richer team data map including attack/defense strengths
    team_data_map = get_team_data_map(teams)
    # Derive the simple short-name map for functions that only needs the name
    teams_map_short = {t_id: d["short_name"] for t_id, d in team_data_map.items()}
    
    # 1. Calculate FDR (Average difficulty of next 3 *fixtures*)
    team_fdr = calculate_fixture_difficulty(fixtures, teams)
    # 2. Build detailed fixture map (Opponent and difficulty strings) - NOW FIXED TO INCLUDE CURRENT GW
    team_fixture_map = build_fixture_map(fixtures, teams_map_short, gw_id)
    # 3. Enrich players with both FDR (for scoring) and fixture map (for display)
    elements = enrich_players(data.get("elements", []), team_fdr, team_fixture_map)
    
    # NEW: Get user's current squad picks
    my_picks = get_my_team_picks(fpl_team_id_str)

    # Time calculations
    now = datetime.now(timezone.utc)
    diff = deadline - now
    days = diff.days
    hours = (diff.seconds // 3600)

    header = (
        f"⚽ *FPL Daily Digest: {gw_name}*\n"
        f"🚨 *DEADLINE:* {deadline.astimezone().strftime('%a %d %b %H:%M %Z')}\n"
        f"⏳ *Time Remaining:* {days}d {hours}h\n"
        "══════════════════════"
    )

    team_summary = get_team_summary(fpl_team_id_str)
    watchlist_text = build_watchlist(elements, teams_map_short)
    # NEW: Captaincy and transfer picks based on team strength metrics (GENERAL)
    # The current_gw_id passed here is the ID of the UPCOMING Gameweek
    captaincy_picks = get_captaincy_picks(team_data_map, fixtures, gw_id)
    # NEW: Personalized analysis for the user's squad (CAPTAINCY & TRANSFERS)
    personal_analysis = get_personal_analysis(my_picks, elements, teams_map_short)
    
    # Returns a list of sections, not a single string
    position_summaries = summarize_players(elements, teams_map_short) 

    # 1. Send Chunk 1 (Header, Team Summary, General Picks, Personalized Analysis, Watchlist)
    # Fixed the string concatenation issue here
    chunk1 = (
        f"{header}\n{team_summary}\n\n" +
        f"{captaincy_picks}\n\n" +
        f"{personal_analysis}\n\n" + # <-- Personalized analysis inserted here
        f"*🔥 High-Priority Watchlist (Top 3 by Position)*\n\n" +
        f"{watchlist_text}\n" +
        f"══════════════════════\n" +
        f"*Detailed Player Statistics will follow in separate messages.*"
    )
    send_telegram_message(chunk1)

    # 2. Send Chunk 2+ (Detailed Stats split by position)
    for section_text in position_summaries:
        send_telegram_message(section_text)

if __name__ == "__main__":
    run_daily_digest()
