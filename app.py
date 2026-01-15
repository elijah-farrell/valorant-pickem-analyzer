from flask import Flask, jsonify, send_from_directory, send_file, request
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from scraper.underdog import get_pickem_slate
from scraper.vlr import (
    find_player_url,
    scrape_current_team,
    scrape_player_name,
    scrape_agent_stats_by_timespan,
    scrape_match_links,
    parse_match_page,
    group_kills_by_match,
    fetch_soup,
    extract_player_links_from_match,
    normalize_name,
    BASE_URL,
    get_team_url_from_player,
    get_match_from_team,
)
import traceback
import os

app = Flask(__name__, static_folder='static', static_url_path='')

# CORS configuration - restrict to your Vercel domain only
# Set ALLOWED_ORIGINS in Render environment variables:
# Format: "https://your-app.vercel.app,https://your-app-git-main.vercel.app"
# Include both production and preview URLs
allowed_origins_env = os.environ.get('ALLOWED_ORIGINS', '').strip()
if allowed_origins_env:
    # Production: restrict to specific domains
    allowed_origins = [origin.strip() for origin in allowed_origins_env.split(',') if origin.strip()]
else:
    # Development: allow all (for local testing)
    allowed_origins = '*'
    
CORS(app, origins=allowed_origins, supports_credentials=True)

# Rate limiting
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"  # In-memory storage (fine for single instance)
)

# Error handler for rate limit exceeded
@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({
        'error': 'Rate limit exceeded',
        'message': 'Too many requests. Please try again later.',
        'retry_after': e.description
    }), 429

MAX_MATCHES = 40

def compute_averages(good_matches, windows=(5, 10, 25)):
    kills = [m['total_kills'] for m in good_matches]
    averages = {}
    for w in windows:
        averages[w] = round(sum(kills[:w]) / w, 2) if len(kills) >= w else None
    return averages

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/health')
@limiter.exempt  # Health check shouldn't be rate limited
def health():
    """Health check endpoint for Render to keep service alive"""
    return jsonify({'status': 'ok', 'service': 'valorant-pickem-analyzer'})

@app.route('/styles.css')
def styles():
    return send_from_directory('static', 'styles.css')

@app.route('/app.js')
def app_js():
    return send_from_directory('static', 'app.js')

@app.route('/favicon.ico')
def favicon():
    return send_from_directory('config', 'valorant.ico')

@app.route('/api/slate', methods=['GET'])
@limiter.limit("10 per minute")  # Limit to 10 requests per minute per IP
def get_slate():
    """Get Underdog pick'em slate with VLR stats comparison"""
    try:
        # Optional manual match URL override (rarely needed)
        match_url = request.args.get('match_url', '').strip()
        
        slate_response = get_pickem_slate()
        if not slate_response or not isinstance(slate_response, dict):
            return jsonify({
                'players': [],
                'match_teams': [],
                'match_url': None,
                'players_by_match': {},
                'message': 'No players found on Underdog. Please check Underdog website for more details.'
            }), 200

        over_under_lines = slate_response.get("over_under_lines", [])
        appearances = slate_response.get("appearances", [])
        players_data = slate_response.get("players", [])
        games = slate_response.get("games", [])
        
        # Build maps for team lookup
        # Map: appearance_id -> (player_id, team_id) from appearances
        # NOTE: team_id in appearance is the player's team, not the opponent
        appearance_to_player_team = {}
        for appearance in appearances:
            appearance_id = appearance.get("id")
            player_id = appearance.get("player_id")
            team_id = appearance.get("team_id")  # This is the player's team
            if appearance_id and player_id and team_id:
                appearance_to_player_team[appearance_id] = (player_id, team_id)
                print(f"[DEBUG] Appearance {appearance_id}: player_id={player_id}, team_id={team_id} (player's team)")
        
        # Map: player_id -> team_id
        player_to_team_id = {}
        for appearance in appearances:
            player_id = appearance.get("player_id")
            team_id = appearance.get("team_id")
            if player_id and team_id:
                player_to_team_id[player_id] = team_id
        
        # Map: player_id -> player name (last_name) and player_id -> team_id
        player_id_to_name = {}
        player_id_to_team_id_direct = {}  # Direct from players array - most reliable!
        for player in players_data:
            player_id = player.get("id")
            player_name = player.get("last_name", "").strip()
            team_id = player.get("team_id")
            if player_id and player_name:
                player_id_to_name[player_id] = player_name
            if player_id and team_id:
                player_id_to_team_id_direct[player_id] = team_id
        
        # Map: team_id -> team name (from games)
        team_id_to_name = {}
        for game in games:
            home_team_id = game.get("home_team_id")
            away_team_id = game.get("away_team_id")
            
            # Try full_team_names_title first (most reliable)
            full_title = game.get("full_team_names_title", "")
            if " vs " in full_title:
                parts = full_title.split(" vs ")
                if len(parts) >= 2:
                    if home_team_id:
                        team_id_to_name[home_team_id] = parts[0].strip()
                    if away_team_id:
                        team_id_to_name[away_team_id] = parts[1].strip()
            # Fallback to title
            elif " vs " in game.get("title", ""):
                parts = game.get("title", "").split(" vs ")
                if len(parts) >= 2:
                    if home_team_id:
                        team_id_to_name[home_team_id] = parts[0].strip()
                    if away_team_id:
                        team_id_to_name[away_team_id] = parts[1].strip()
            # Fallback to short_title if available
            elif " vs " in game.get("short_title", ""):
                parts = game.get("short_title", "").split(" vs ")
                if len(parts) >= 2:
                    if home_team_id:
                        team_id_to_name[home_team_id] = parts[0].strip()
                    if away_team_id:
                        team_id_to_name[away_team_id] = parts[1].strip()
        
        # Also build team_id_to_name from appearances if we have player_id -> team_id mapping
        # This ensures we get team names even if game titles don't have them
        for player_id, team_id in player_to_team_id.items():
            if team_id and team_id not in team_id_to_name:
                # Try to find team name from any game this team appears in
                for game in games:
                    if game.get("home_team_id") == team_id or game.get("away_team_id") == team_id:
                        # We already processed this game above, so skip
                        break
        
        print(f"[DEBUG] Mapped {len(team_id_to_name)} team IDs to names: {team_id_to_name}")
        
        # Map: player name -> team name
        player_name_to_team = {}
        for player_id, team_id in player_to_team_id.items():
            player_name = player_id_to_name.get(player_id)
            team_name = team_id_to_name.get(team_id)
            if player_name and team_name:
                # Store with normalized key for case-insensitive matching
                player_name_clean = player_name.strip()
                player_name_to_team[player_name_clean] = team_name
                player_name_to_team[player_name_clean.lower()] = team_name  # Also store lowercase for matching
        
        print(f"[DEBUG] Mapped {len(player_name_to_team)} players to teams")
        
        # Extract player info from over_under_lines
        player_info = []
        processed_player_names = set()
        for item in over_under_lines:
            try:
                # Check if the item has the expected structure
                if not item.get("over_under") or not item["over_under"].get("title"):
                    continue
                    
                if "Kills on Maps 1+2 O/U" in item["over_under"]["title"]:
                    player = item["over_under"]["title"].replace(" Kills on Maps 1+2 O/U", "").strip()
                    processed_player_names.add(player)
                    line = item.get('stat_value')
                    options = item.get("options", [])
                    odds_over = options[0].get("american_price", "N/A") if len(options) >= 1 else "N/A"
                    odds_under = options[1].get("american_price", "N/A") if len(options) >= 2 else "N/A"
                    
                    # Get team directly from players array using player name -> player_id -> team_id
                    team = None
                    player_normalized = player.strip()
                    
                    # Find player_id from name
                    player_id = None
                    for pid, pname in player_id_to_name.items():
                        if pname.strip() == player_normalized or pname.strip().lower() == player_normalized.lower():
                            player_id = pid
                            break
                    
                    # Get team_id directly from players array (most reliable!)
                    if player_id and player_id in player_id_to_team_id_direct:
                        team_id = player_id_to_team_id_direct[player_id]
                        team = team_id_to_name.get(team_id)
                        print(f"[DEBUG] Player '{player}': player_id={player_id}, team_id={team_id}, team_name={team} (from players array)")
                    
                    # Fallback: Get team from player_name_to_team map
                    if not team:
                        team = player_name_to_team.get(player_normalized) or player_name_to_team.get(player_normalized.lower())
                        print(f"[DEBUG] Player '{player}': Using fallback team lookup, found: {team}")
                    
                    # Get match_id for this player from appearance
                    match_id = None
                    appearance_stat = item.get("over_under", {}).get("appearance_stat", {})
                    appearance_id = appearance_stat.get("appearance_id")
                    if appearance_id:
                        for appearance in appearances:
                            if appearance.get("id") == appearance_id:
                                match_id = appearance.get("match_id")
                                break
                    
                    player_info.append({
                        'player': player,
                        'line': line,
                        'odds_over': odds_over,
                        'odds_under': odds_under,
                        'team': team,  # Team from Underdog API
                        'match_id': match_id  # Store match_id for organization
                    })
            except Exception as e:
                print(f"Error parsing slate item: {e}")
                continue
        
        print(f"[DEBUG] Processed {len(player_info)} players from {len(over_under_lines)} over_under_lines")
        print(f"[DEBUG] Player names processed: {sorted(processed_player_names)}")

        # Organize players by match using match_id from appearances (all from Underdog API - no VLR scraping!)
        match_teams = []
        all_matches_info = []
        
        if len(player_info) > 0 and len(games) > 0:
            print(f"[DEBUG] Organizing {len(player_info)} players by matches from Underdog API (no VLR scraping needed)")
            
            # Map: match_id -> game info
            match_id_to_game = {}
            for game in games:
                match_id = game.get("id")
                if match_id:
                    match_id_to_game[match_id] = game
            
            # Organize by matches - create match info from games
            for game in games:
                match_id = game.get("id")
                home_team_id = game.get("home_team_id")
                away_team_id = game.get("away_team_id")
                home_team_name = team_id_to_name.get(home_team_id)
                away_team_name = team_id_to_name.get(away_team_id)
                
                if match_id and home_team_name and away_team_name:
                    match_key = f"{home_team_name} vs {away_team_name}"
                    all_matches_info.append({
                        'url': None,  # No VLR URL needed - all from Underdog API
                        'teams': [home_team_name, away_team_name],
                        'match_key': match_key,
                        'match_id': match_id
                    })
                    
                    # Set primary match teams if not set
                    if not match_teams:
                        match_teams = [home_team_name, away_team_name]
            
            print(f"[DEBUG] Organized into {len(all_matches_info)} matches from Underdog API")

        # Fetch VLR stats for each player
        results = []
        for player_data in player_info:
            player = player_data['player']
            line = player_data['line']
            # Use team from Underdog API (never guess/scrape from VLR)
            team_from_underdog = player_data.get('team')
            
            try:
                # Always search for player URL directly using name search
                url = find_player_url(player)
                if not url:
                    results.append({
                        'player': player,
                        'line': line,
                        'odds_over': player_data['odds_over'],
                        'odds_under': player_data['odds_under'],
                        'team': team_from_underdog,  # Use Underdog team as fallback when VLR not available
                        'team_url': None,
                        'vlr_url': None,
                        'avg_last_5': None,
                        'avg_last_10': None,
                        'avg_last_25': None,
                        'error': 'Player not found on VLR.gg'
                    })
                    continue

                soup = fetch_soup(url)
                if not soup:
                    # Get team URL for linking (but always use team name from Underdog API)
                    team_url = None
                    if url:
                        try:
                            team_url = get_team_url_from_player(url)
                        except:
                            pass
                    
                    results.append({
                        'player': player,
                        'line': line,
                        'odds_over': player_data['odds_over'],
                        'odds_under': player_data['odds_under'],
                        'team': team_from_underdog,  # Use team from Underdog API
                        'team_url': team_url,
                        'vlr_url': url,
                        'avg_last_5': None,
                        'avg_last_10': None,
                        'avg_last_25': None,
                        'error': 'Failed to fetch player page'
                    })
                    continue

                # Get team URL for linking (but always use team name from Underdog API)
                team_url = None
                try:
                    team_url = get_team_url_from_player(url)
                except:
                    pass

                # Scrape match data
                links = scrape_match_links(url)
                if not links:
                    results.append({
                        'player': player,
                        'line': line,
                        'odds_over': player_data['odds_over'],
                        'odds_under': player_data['odds_under'],
                        'team': team_from_underdog,  # Use team from Underdog API
                        'team_url': team_url,
                        'vlr_url': url,
                        'avg_last_5': None,
                        'avg_last_10': None,
                        'avg_last_25': None,
                        'error': 'No match history found'
                    })
                    continue

                all_maps = []
                for link in links[:MAX_MATCHES]:
                    try:
                        maps = parse_match_page(link, player)
                        all_maps.extend(maps)
                    except Exception as e:
                        print(f"Error parsing match {link}: {e}")
                        continue

                good_matches = group_kills_by_match(all_maps, player, max_maps=2)
                if not good_matches:
                    results.append({
                        'player': player,
                        'line': line,
                        'odds_over': player_data['odds_over'],
                        'odds_under': player_data['odds_under'],
                        'team': team_from_underdog,  # Use team from Underdog API
                        'team_url': team_url,
                        'vlr_url': url,
                        'avg_last_5': None,
                        'avg_last_10': None,
                        'avg_last_25': None,
                        'error': 'No valid matches found (need matches with exactly 2 maps)'
                    })
                    continue

                avgs = compute_averages(good_matches)
                
                # Get team URL if we have player URL
                if not team_url:
                    try:
                        team_url = get_team_url_from_player(url)
                    except:
                        pass
                
                results.append({
                    'player': player,
                    'line': line,
                    'odds_over': player_data['odds_over'],
                    'odds_under': player_data['odds_under'],
                    'team': team_from_underdog,  # Use team from Underdog API
                    'team_url': team_url,
                    'vlr_url': url,
                    'avg_last_5': avgs[5],
                    'avg_last_10': avgs[10],
                    'avg_last_25': avgs[25],
                    'matches_analyzed': len(good_matches),
                    'match_id': player_data.get('match_id')  # Store match_id for organization
                })
            except Exception as e:
                error_details = traceback.format_exc()
                print(f"Error processing player {player}: {error_details}")
                # Extract a more user-friendly error message
                error_msg = str(e)
                if "timeout" in error_msg.lower():
                    error_msg = "Request timeout - VLR.gg may be slow or unavailable"
                elif "connection" in error_msg.lower():
                    error_msg = "Connection error - cannot reach VLR.gg"
                elif "404" in error_msg or "not found" in error_msg.lower():
                    error_msg = "Player page not found on VLR.gg"
                else:
                    error_msg = f"Scraping error: {error_msg[:100]}"  # Limit length
                
                results.append({
                    'player': player,
                    'line': line,
                    'odds_over': player_data['odds_over'],
                    'odds_under': player_data['odds_under'],
                    'team': team_from_underdog,  # Use Underdog team as fallback when VLR not available
                    'team_url': None,
                    'vlr_url': None,
                    'avg_last_5': None,
                    'avg_last_10': None,
                    'avg_last_25': None,
                    'error': error_msg
                })

        # Organize players by match using match_id from results (all from Underdog API - no VLR scraping!)
        players_by_match = {}  # Map match_key -> {teams: [team1, team2], players: []}
        
        if len(all_matches_info) > 0:
            print(f"[DEBUG] Organizing {len(results)} players into {len(all_matches_info)} matches using Underdog API data")
            
            # Map: match_id -> match_info for quick lookup
            match_id_to_match_info = {}
            for match_info in all_matches_info:
                match_id = match_info.get('match_id')
                if match_id:
                    match_id_to_match_info[match_id] = match_info
            
            # Group results by match_id
            match_id_to_results = {}
            for player_result in results:
                match_id = player_result.get('match_id')
                if match_id:
                    if match_id not in match_id_to_results:
                        match_id_to_results[match_id] = []
                    match_id_to_results[match_id].append(player_result)
            
            # For each match, organize players by team
            for match_info in all_matches_info:
                match_id = match_info.get('match_id')
                match_key = match_info.get('match_key')
                teams = match_info.get('teams', [])
                
                if not match_key or len(teams) < 2:
                    continue
                
                team1 = teams[0]
                team2 = teams[1]
                
                if match_key not in players_by_match:
                    players_by_match[match_key] = {
                        'teams': [team1, team2],
                        'players': []
                    }
                
                # Get players for this match_id
                match_players = match_id_to_results.get(match_id, [])
                
                # Sort players by team: team1 players first, then team2 players
                team1_players = []
                team2_players = []
                
                for player_result in match_players:
                    player_team = player_result.get('team', '')
                    if not player_team:
                        # Try to get team from player_id if team is missing
                        player_name = player_result.get('player', '').strip()
                        if player_name:
                            # Look up player_id from name
                            player_id = None
                            for pid, pname in player_id_to_name.items():
                                if pname.strip() == player_name:
                                    player_id = pid
                                    break
                            
                            if player_id and player_id in player_to_team_id:
                                team_id = player_to_team_id[player_id]
                                player_team = team_id_to_name.get(team_id, '')
                                player_result['team'] = player_team  # Update the result
                    
                    normalized_player_team = normalize_name(player_team) if player_team else ''
                    normalized_team1 = normalize_name(team1)
                    normalized_team2 = normalize_name(team2)
                    
                    # More robust team matching
                    team_matched = False
                    if normalized_player_team:
                        if (normalized_player_team == normalized_team1 or 
                            normalized_team1 == normalized_player_team or
                            (normalized_team1 and normalized_team1 in normalized_player_team) or
                            (normalized_player_team and normalized_player_team in normalized_team1)):
                            team1_players.append(player_result)
                            team_matched = True
                        elif (normalized_player_team == normalized_team2 or 
                              normalized_team2 == normalized_player_team or
                              (normalized_team2 and normalized_team2 in normalized_player_team) or
                              (normalized_player_team and normalized_player_team in normalized_team2)):
                            team2_players.append(player_result)
                            team_matched = True
                    
                    if not team_matched:
                        # If can't determine, try to match by checking the actual game teams
                        # Get the game for this match
                        game = match_id_to_game.get(match_id)
                        if game:
                            home_team_id = game.get("home_team_id")
                            away_team_id = game.get("away_team_id")
                            
                            # Try to get player's team_id from player_id
                            player_name = player_result.get('player', '').strip()
                            player_id = None
                            for pid, pname in player_id_to_name.items():
                                if pname.strip() == player_name:
                                    player_id = pid
                                    break
                            
                            if player_id and player_id in player_to_team_id:
                                player_team_id = player_to_team_id[player_id]
                                if player_team_id == home_team_id:
                                    team1_players.append(player_result)
                                elif player_team_id == away_team_id:
                                    team2_players.append(player_result)
                                else:
                                    # Default to team1 if still can't determine
                                    team1_players.append(player_result)
                            else:
                                # Default to team1 if can't determine
                                team1_players.append(player_result)
                        else:
                            # Default to team1 if no game info
                            team1_players.append(player_result)
                
                # Add team1 players first, then team2 players
                players_by_match[match_key]['players'].extend(team1_players)
                players_by_match[match_key]['players'].extend(team2_players)
                
                print(f"[DEBUG] Match '{match_key}': {len(team1_players)} team1 players, {len(team2_players)} team2 players")
            
            # Add any remaining players (not in any match) to "Other"
            all_assigned_player_names = set()
            for match_data in players_by_match.values():
                for player in match_data['players']:
                    player_name = player.get('player', '').strip()
                    if player_name:
                        all_assigned_player_names.add(player_name)
            
            for player_result in results:
                player_name = player_result.get('player', '').strip()
                if player_name and player_name not in all_assigned_player_names:
                    if 'Other' not in players_by_match:
                        players_by_match['Other'] = {
                            'teams': [],
                            'players': []
                        }
                    players_by_match['Other']['players'].append(player_result)
                    all_assigned_player_names.add(player_name)
        else:
            # No match info - add all players to a single group
            if results:
                players_by_match['All Players'] = {
                    'teams': [],
                    'players': results
                }
        
        # Debug: Print team assignments for verification
        print(f"[DEBUG] Final team assignments for {len(results)} players:")
        for player_result in results[:10]:  # Print first 10 for debugging
            player_name = player_result.get('player', '')
            team = player_result.get('team', 'N/A')
            match_id = player_result.get('match_id', 'N/A')
            print(f"  {player_name}: team={team}, match_id={match_id}")
        if len(results) > 10:
            print(f"  ... and {len(results) - 10} more players")
        
        return jsonify({
            'players': results,
            'match_teams': match_teams,
            'match_url': match_url if match_url else None,
            'players_by_match': players_by_match
        })
    except Exception as e:
        error_msg = f"Error fetching slate: {str(e)}"
        print(f"Slate endpoint error: {traceback.format_exc()}")
        return jsonify({'error': error_msg, 'details': str(e)}), 500

@app.route('/api/player/<player_name>', methods=['GET'])
@limiter.limit("20 per minute")  # Limit to 20 requests per minute per IP
def get_player_stats(player_name):
    """Get detailed stats for a specific player"""
    try:
        url = find_player_url(player_name)
        if not url:
            return jsonify({'error': f'Player "{player_name}" not found on VLR.gg'}), 404

        print(f"[DEBUG] Player URL found: {url}")
        soup = fetch_soup(url)
        if not soup:
            return jsonify({'error': 'Failed to fetch player page from VLR.gg'}), 500

        # Get the actual player display name from VLR
        actual_player_name = scrape_player_name(soup)
        if not actual_player_name:
            actual_player_name = player_name  # Fallback to search term if can't find name
        
        team = scrape_current_team(soup)
        
        # Get team URL from player profile
        team_url = None
        try:
            team_url = get_team_url_from_player(url)
        except:
            pass

        # Get agent stats (optional, don't fail if it doesn't work)
        agent_stats = {}
        try:
            agent_stats = scrape_agent_stats_by_timespan(url)
        except Exception as e:
            pass  # Agent stats are nice to have but not critical

        # Get match data
        links = scrape_match_links(url)
        print(f"[DEBUG] Found {len(links)} match links for {player_name}")
        if not links:
            return jsonify({
                'player': actual_player_name,
                'team': team,
                'team_url': team_url,
                'vlr_url': url,
                'agent_stats': agent_stats,
                'matches': [],
                'averages': {
                    'last_5': None,
                    'last_10': None,
                    'last_25': None
                },
                'error': 'No match history found',
                'debug': {'links_found': 0}
            })

        all_maps = []
        for link in links[:MAX_MATCHES]:
            try:
                maps = parse_match_page(link, player_name)
                print(f"[DEBUG] Parsed {len(maps)} maps from {link}")
                all_maps.extend(maps)
            except Exception as e:
                print(f"[DEBUG] Error parsing match {link}: {e}")
                continue  # Skip problematic matches

        print(f"[DEBUG] Total maps scraped: {len(all_maps)}")
        good_matches = group_kills_by_match(all_maps, player_name, max_maps=2)
        print(f"[DEBUG] Good matches found: {len(good_matches)}")
        avgs = compute_averages(good_matches)
        print(f"[DEBUG] Averages: {avgs}")

        return jsonify({
            'player': actual_player_name,
            'team': team,
            'team_url': team_url,
            'vlr_url': url,
            'agent_stats': agent_stats,
            'matches': good_matches,
            'averages': {
                'last_5': avgs[5],
                'last_10': avgs[10],
                'last_25': avgs[25]
            },
            'matches_found': len(good_matches),
            'maps_scraped': len(all_maps),
            'debug': {
                'links_found': len(links),
                'links_checked': min(len(links), MAX_MATCHES),
                'all_maps_count': len(all_maps),
                'good_matches_count': len(good_matches)
            }
        })
    except Exception as e:
        return jsonify({'error': f'Error processing player: {str(e)}'}), 500

if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_ENV') != 'production'
    app.run(debug=debug, host='0.0.0.0', port=port)

