import os

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from scraper.underdog import get_pickem_slate
from scraper.vlr import (
    find_player_url,
    scrape_current_team,
    scrape_player_name,
    scrape_match_links,
    parse_match_page,
    group_kills_by_match,
    fetch_soup,
    get_team_url_from_player,
)

# Detect if we're in development mode
# Production (Render) will have ALLOWED_ORIGINS set
is_development = not os.environ.get('ALLOWED_ORIGINS', '').strip()

# Flask app - API backend only in production, serves frontend in dev
app = Flask(__name__, static_folder='static' if is_development else None, static_url_path='')

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


def get_player_vlr_kill_averages(player_name):
    """
    Shared VLR pipeline: find player -> fetch page -> scrape match links -> parse matches -> kill averages.
    Returns dict with vlr_url, team_url, avg_last_5/10/25, good_matches, soup (for reuse), and error if any.
    Used by both /api/slate and /api/player.
    """
    out = {
        'vlr_url': None,
        'team_url': None,
        'avg_last_5': None,
        'avg_last_10': None,
        'avg_last_25': None,
        'good_matches': [],
        'soup': None,
        'error': None,
    }
    url = find_player_url(player_name)
    if not url:
        out['error'] = 'Player not found on VLR.gg'
        return out
    soup = fetch_soup(url)
    if not soup:
        out['vlr_url'] = url
        out['error'] = 'Failed to fetch player page'
        try:
            out['team_url'] = get_team_url_from_player(url)
        except Exception:
            pass
        return out
    out['vlr_url'] = url
    out['soup'] = soup
    try:
        out['team_url'] = get_team_url_from_player(url)
    except Exception:
        pass
    links = scrape_match_links(url)
    if not links:
        out['error'] = 'No match history found'
        out['links_found'] = 0
        out['all_maps_count'] = 0
        return out
    all_maps = []
    for link in links[:MAX_MATCHES]:
        try:
            maps = parse_match_page(link, player_name)
            all_maps.extend(maps)
        except Exception:
            continue
    out['links_found'] = len(links)
    out['all_maps_count'] = len(all_maps)
    good_matches = group_kills_by_match(all_maps, player_name, max_maps=2)
    if not good_matches:
        out['error'] = 'No valid matches found (need matches with exactly 2 maps)'
        return out
    avgs = compute_averages(good_matches)
    out['avg_last_5'] = avgs[5]
    out['avg_last_10'] = avgs[10]
    out['avg_last_25'] = avgs[25]
    out['good_matches'] = good_matches
    return out


@app.route('/')
def index():
    """Root endpoint - serves frontend in dev, API info in production"""
    if is_development:
        # Development: serve the frontend HTML
        return send_from_directory('static', 'index.html')
    else:
        # Production: API info only
        return jsonify({
            'service': 'Valorant Pick\'em Analyzer API',
            'endpoints': {
                'health': '/health',
                'slate': '/api/slate',
                'player': '/api/player/<player_name>'
            }
        })

# Development-only routes for serving static files
if is_development:
    @app.route('/<path:path>')
    def serve_static(path):
        """Serve static files in development mode only"""
        # Don't serve API routes as static files
        if path.startswith('api/') or path == 'health':
            return jsonify({'error': 'Not found'}), 404
        try:
            return send_from_directory('static', path)
        except:
            return jsonify({'error': 'Not found'}), 404

@app.route('/health')
@limiter.exempt  # Health check shouldn't be rate limited
def health():
    """Health check endpoint for Render to keep service alive"""
    return jsonify({'status': 'ok', 'service': 'valorant-pickem-analyzer'})

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
        # NOTE: appearance.team_id is the OPPONENT in this API; we derive the player's team from game home/away.
        appearance_to_player_team = {}
        for appearance in appearances:
            appearance_id = appearance.get("id")
            player_id = appearance.get("player_id")
            team_id = appearance.get("team_id")
            if appearance_id and player_id and team_id:
                appearance_to_player_team[appearance_id] = (player_id, team_id)
        
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
        
        # Map: match_id -> game (needed before over_under loop to derive player's actual team)
        match_id_to_game = {}
        for game in games:
            gid = game.get("id")
            if gid:
                match_id_to_game[gid] = game
        
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
                    
                    player_normalized = player.strip()
                    player_id = None
                    team_id = None
                    team = None
                    match_id = None

                    # Resolve appearance for this over_under line (has match_id, player_id, team_id)
                    appearance_stat = item.get("over_under", {}).get("appearance_stat", {})
                    appearance_id = appearance_stat.get("appearance_id")
                    appearance = None
                    if appearance_id:
                        for a in appearances:
                            if a.get("id") == appearance_id:
                                appearance = a
                                match_id = a.get("match_id")
                                break

                    if appearance:
                        player_id = appearance.get("player_id")
                        appearance_team_id = appearance.get("team_id")
                        game = match_id_to_game.get(match_id) if match_id else None
                        # Underdog API: appearance.team_id is the OPPONENT (team they're playing), not the player's team.
                        if game and appearance_team_id:
                            home_id = game.get("home_team_id")
                            away_id = game.get("away_team_id")
                            if appearance_team_id == home_id:
                                team_id = away_id
                                team = team_id_to_name.get(away_id)
                            elif appearance_team_id == away_id:
                                team_id = home_id
                                team = team_id_to_name.get(home_id)

                    # Fallback: player_id from name if not from appearance
                    if not player_id:
                        for pid, pname in player_id_to_name.items():
                            if pname.strip() == player_normalized or pname.strip().lower() == player_normalized.lower():
                                player_id = pid
                                break
                    # Fallback: team from players array, then appearance.team_id
                    if not team_id and player_id and player_id in player_id_to_team_id_direct:
                        team_id = player_id_to_team_id_direct[player_id]
                        team = team_id_to_name.get(team_id)
                    if not team_id and appearance and appearance.get("team_id"):
                        team_id = appearance.get("team_id")
                        team = team_id_to_name.get(team_id)

                    # Skip if we can't get team_id from API
                    if not team_id:
                        continue
                    
                    if not team:
                        continue
                    
                    player_info.append({
                        'player': player,
                        'line': line,
                        'odds_over': odds_over,
                        'odds_under': odds_under,
                        'team': team,  # Team name from Underdog API
                        'team_id': team_id,  # Team ID from Underdog API (for reliable matching)
                        'player_id': player_id,  # Store player_id for reliable lookup
                        'match_id': match_id  # Store match_id for organization
                    })
            except Exception as e:
                continue

        # Organize players by match using match_id from appearances (all from Underdog API - no VLR scraping!)
        match_teams = []
        all_matches_info = []
        
        if len(player_info) > 0 and len(games) > 0:
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

        # Fetch VLR stats for each player (shared pipeline: get_player_vlr_kill_averages)
        results = []
        for player_data in player_info:
            player = player_data['player']
            line = player_data['line']
            # Use team from Underdog API (never guess/scrape from VLR)
            team_from_underdog = player_data.get('team')
            team_id_from_underdog = player_data.get('team_id')  # Store team_id for reliable matching
            player_id_from_underdog = player_data.get('player_id')  # Store player_id for reliable lookup
            
            try:
                r = get_player_vlr_kill_averages(player)
                row = {
                    'player': player,
                    'line': line,
                    'odds_over': player_data['odds_over'],
                    'odds_under': player_data['odds_under'],
                    'team': team_from_underdog,
                    'team_id': team_id_from_underdog,
                    'player_id': player_id_from_underdog,
                    'team_url': r['team_url'],
                    'vlr_url': r['vlr_url'],
                    'avg_last_5': r['avg_last_5'],
                    'avg_last_10': r['avg_last_10'],
                    'avg_last_25': r['avg_last_25'],
                    'match_id': player_data.get('match_id'),
                }
                if r['error']:
                    row['error'] = r['error']
                else:
                    row['matches_analyzed'] = len(r['good_matches'])
                results.append(row)
            except Exception as e:
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
                    'team': team_from_underdog,  # Use Underdog team from API
                    'team_id': team_id_from_underdog,  # Store team_id from API
                    'player_id': player_id_from_underdog,  # Store player_id from API
                    'team_url': None,
                    'vlr_url': None,
                    'avg_last_5': None,
                    'avg_last_10': None,
                    'avg_last_25': None,
                    'error': error_msg,
                    'match_id': player_data.get('match_id')
                })

        # Organize players by match using match_id from results (all from Underdog API - no VLR scraping!)
        players_by_match = {}  # Map match_key -> {teams: [team1, team2], players: []}
        
        if len(all_matches_info) > 0:
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
                # Use team_id directly from API - straightforward comparison
                team1_players = []
                team2_players = []
                
                # Get the game for this match to access team IDs
                game = match_id_to_game.get(match_id)
                if not game:
                    continue  # Skip if no game info
                
                home_team_id = game.get("home_team_id")
                away_team_id = game.get("away_team_id")
                
                for player_result in match_players:
                    # Get team_id directly from player_result (stored from API)
                    player_team_id = player_result.get('team_id')
                    
                    if not player_team_id:
                        continue
                    
                    # Assign player to correct team based on team_id comparison
                    if player_team_id == home_team_id:
                        team1_players.append(player_result)
                    elif player_team_id == away_team_id:
                        team2_players.append(player_result)
                    # If team_id doesn't match, skip (likely data inconsistency)
                
                # Add team1 players first, then team2 players
                players_by_match[match_key]['players'].extend(team1_players)
                players_by_match[match_key]['players'].extend(team2_players)
            
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
        
        
        return jsonify({
            'players': results,
            'match_teams': match_teams,
            'match_url': match_url if match_url else None,
            'players_by_match': players_by_match
        })
    except Exception as e:
        error_msg = "Unable to fetch slate data. Please try again later."
        return jsonify({'error': error_msg}), 500

@app.route('/api/player/<player_name>', methods=['GET'])
@limiter.limit("20 per minute")  # Limit to 20 requests per minute per IP
def get_player_stats(player_name):
    """Get detailed stats for a specific player. Uses shared get_player_vlr_kill_averages + VLR-only extras (name, team)."""
    try:
        r = get_player_vlr_kill_averages(player_name)

        if r['error'] and not r['vlr_url']:
            return jsonify({'error': f'Player "{player_name}" not found on VLR.gg'}), 404
        if r['error'] == 'Failed to fetch player page':
            return jsonify({'error': 'Failed to fetch player page from VLR.gg'}), 500

        actual_player_name = (scrape_player_name(r['soup']) or player_name) if r['soup'] else player_name
        team = scrape_current_team(r['soup']) if r['soup'] else None
        team_url = r['team_url']
        vlr_url = r['vlr_url']

        links_found = r.get('links_found', 0)
        all_maps_count = r.get('all_maps_count', 0)
        good_matches = r['good_matches']
        debug_info = {
            'links_found': links_found,
            'links_checked': min(links_found, MAX_MATCHES),
            'all_maps_count': all_maps_count,
            'good_matches_count': len(good_matches),
        }

        payload = {
            'player': actual_player_name,
            'team': team,
            'team_url': team_url,
            'vlr_url': vlr_url,
            'matches': good_matches,
            'averages': {
                'last_5': r['avg_last_5'],
                'last_10': r['avg_last_10'],
                'last_25': r['avg_last_25'],
            },
            'matches_found': len(good_matches),
            'maps_scraped': all_maps_count,
            'debug': debug_info,
        }
        if r['error']:
            payload['error'] = r['error']
        return jsonify(payload)
    except Exception as e:
        return jsonify({'error': 'Unable to fetch player stats. Please try again later.'}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_ENV') != 'production'
    app.run(debug=debug, host='0.0.0.0', port=port)

