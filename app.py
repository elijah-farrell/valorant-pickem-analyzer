from flask import Flask, jsonify, send_from_directory, send_file, request
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from scraper.underdog import get_pickem_slate
from scraper.vlr import (
    find_player_url,
    scrape_current_team,
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
        
        slate = get_pickem_slate()
        if not slate:
            return jsonify({
                'players': [],
                'match_teams': [],
                'match_url': None,
                'players_by_match': {},
                'message': 'No players found on Underdog. Please check Underdog website for more details.'
            }), 200

        player_info = []
        for item in slate:
            try:
                # Check if the item has the expected structure
                if not item.get("over_under") or not item["over_under"].get("title"):
                    continue
                    
                if "Kills on Maps 1+2 O/U" in item["over_under"]["title"]:
                    player = item["over_under"]["title"].replace(" Kills on Maps 1+2 O/U", "").strip()
                    line = item.get('stat_value')
                    options = item.get("options", [])
                    odds_over = options[0].get("american_price", "N/A") if len(options) >= 1 else "N/A"
                    odds_under = options[1].get("american_price", "N/A") if len(options) >= 2 else "N/A"
                    player_info.append({
                        'player': player,
                        'line': line,
                        'odds_over': odds_over,
                        'odds_under': odds_under
                    })
            except Exception as e:
                print(f"Error parsing slate item: {e}")
                continue

        # Step 1: Get teams for ALL players to identify which teams are playing
        # Step 2: Find next match for each team (the next game they play)
        # Step 3: Extract all player links from those matches
        # Step 4: Process all players using those links
        
        player_link_map = {}
        match_teams = []
        all_matches_info = []  # Store all matches with their teams: [{url, teams: [team1, team2]}, ...]
        match_urls_found = []  # Store all match URLs we found
        use_match_link = False  # Flag: True = use player links from match pages, False = fall back to name search
        
        # If we have a manually provided match URL, use it first
        if match_url:
            try:
                print(f"[DEBUG] Using provided match URL: {match_url}")
                match_data = extract_player_links_from_match(match_url)
                extracted_players = match_data.get('players', {})
                for key, value in extracted_players.items():
                    player_link_map[key] = value
                match_teams = match_data.get('teams', [])
                match_urls_found.append(match_url)
                use_match_link = True
            except Exception as e:
                print(f"[DEBUG] Failed to extract from provided match URL: {e}")
        
        # Step 1: Get teams for ALL players to identify which teams are playing
        if not use_match_link and len(player_info) > 0:
            print(f"[DEBUG] Step 1: Getting teams for ALL {len(player_info)} players")
            teams_to_players = {}  # Map team -> list of players
            teams_checked = set()
            
            # Get teams for ALL players
            for player_data in player_info:
                try:
                    player = player_data['player']
                    print(f"[DEBUG] Getting team for: {player}")
                    
                    # Find player URL
                    player_url = find_player_url(player)
                    if not player_url:
                        continue
                    
                    # Get team name and URL from player page
                    soup = fetch_soup(player_url)
                    if not soup:
                        continue
                    
                    team_name = scrape_current_team(soup)
                    if not team_name:
                        continue
                    
                    # Get team URL
                    team_url = get_team_url_from_player(player_url)
                    if not team_url:
                        continue
                    
                    # Group players by team
                    if team_name not in teams_to_players:
                        teams_to_players[team_name] = {
                            'players': [],
                            'team_url': team_url
                        }
                    teams_to_players[team_name]['players'].append(player)
                    
                except Exception as e:
                    print(f"[DEBUG] Error getting team for {player_data.get('player', 'unknown')}: {e}")
                    continue
            
            print(f"[DEBUG] Found {len(teams_to_players)} teams: {list(teams_to_players.keys())}")
            
            # Step 2: Find next match for each team
            if len(teams_to_players) > 0:
                print(f"[DEBUG] Step 2: Finding next match for {len(teams_to_players)} teams")
                all_match_urls = []
                match_teams_map = {}  # Map match_url -> [team1, team2]
                
                for team_name, team_data in teams_to_players.items():
                    team_url = team_data['team_url']
                    if team_url in teams_checked:
                        continue
                    teams_checked.add(team_url)
                    
                    print(f"[DEBUG] Getting next match for {team_name} ({team_url})")
                    next_matches = get_match_from_team(team_url)
                    print(f"[DEBUG] Found {len(next_matches)} match(es) - using first one")
                    
                    # Only get the first/next match per team
                    for match_url_found in next_matches[:1]:  # Only get the next match
                        if match_url_found not in all_match_urls:
                            all_match_urls.append(match_url_found)
                            # Get teams from match to map them
                            try:
                                match_data = extract_player_links_from_match(match_url_found)
                                match_teams_from_match = match_data.get('teams', [])
                                if len(match_teams_from_match) >= 2:
                                    match_teams_map[match_url_found] = match_teams_from_match[:2]
                            except:
                                pass
                
                print(f"[DEBUG] Found {len(all_match_urls)} total upcoming matches")
                
                # Step 3: Extract player links from all found matches
                print(f"[DEBUG] Step 3: Extracting player links from matches")
                for match_url_found in all_match_urls[:10]:  # Limit to 10 matches total
                    try:
                        match_data = extract_player_links_from_match(match_url_found)
                        extracted_players = match_data.get('players', {})
                        
                        # Check if this match has any of our players
                        has_our_players = False
                        for p_data in player_info:
                            normalized_player = normalize_name(p_data['player'])
                            if normalized_player in extracted_players:
                                has_our_players = True
                                break
                            # Also check display names
                            for key, link_data in extracted_players.items():
                                display_name = normalize_name(link_data.get('display_name', ''))
                                if normalized_player in display_name or display_name in normalized_player:
                                    has_our_players = True
                                    break
                            if has_our_players:
                                break
                        
                        if has_our_players:
                            print(f"[DEBUG] Found relevant match: {match_url_found}")
                            match_urls_found.append(match_url_found)
                            
                            # Merge into player_link_map
                            for key, value in extracted_players.items():
                                if key not in player_link_map:
                                    player_link_map[key] = value
                            
                            # Get teams from this match and store match info
                            match_teams_from_match = match_data.get('teams', [])
                            if match_teams_from_match:
                                all_matches_info.append({
                                    'url': match_url_found,
                                    'teams': match_teams_from_match[:2]
                                })
                                # Use first match's teams as primary (for backward compatibility)
                                if not match_teams:
                                    match_teams = match_teams_from_match[:2]
                            
                            use_match_link = True
                    except Exception as e:
                        print(f"[DEBUG] Failed to extract from match {match_url_found}: {e}")
                        # Even if we can't extract player links, if we have team info from earlier, use it for organization
                        if match_url_found in match_teams_map:
                            teams_from_map = match_teams_map[match_url_found]
                            if len(teams_from_map) >= 2:
                                all_matches_info.append({
                                    'url': match_url_found,
                                    'teams': teams_from_map[:2]
                                })
                                if not match_teams:
                                    match_teams = teams_from_map[:2]
                        continue
        
        print(f"[DEBUG] Step 4: Found {len(player_link_map)} players via {len(match_urls_found)} matches")
        if not player_link_map:
            print("[DEBUG] No matches found, will use name search fallback")
            use_match_link = False

        # Fetch VLR stats for each player
        results = []
        for player_data in player_info:
            player = player_data['player']
            line = player_data['line']
            
            try:
                # Try to find player URL using match link method first
                url = None
                team = None
                normalized_player = normalize_name(player)
                
                if use_match_link and player_link_map:
                    # Try exact match first
                    if normalized_player in player_link_map:
                        url = player_link_map[normalized_player]['url']
                        team = player_link_map[normalized_player].get('team')
                        print(f"[DEBUG] Found {player} via match link: {url}")
                    else:
                        # Try partial match (player name might be in the display name)
                        for normalized_key, link_data in player_link_map.items():
                            display_name = normalize_name(link_data.get('display_name', ''))
                            if normalized_player in display_name or display_name in normalized_player:
                                url = link_data['url']
                                team = link_data.get('team')
                                print(f"[DEBUG] Found {player} via partial match: {url}")
                                break
                
                # Fall back to name search if match link method didn't work
                if not url:
                    url = find_player_url(player)
                    if not url:
                        results.append({
                        'player': player,
                        'line': line,
                        'odds_over': player_data['odds_over'],
                        'odds_under': player_data['odds_under'],
                        'team': None,
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
                    # Get team URL if we have player URL
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
                        'team': team,  # Use team from match if available
                        'team_url': team_url,
                        'vlr_url': url,
                        'avg_last_5': None,
                        'avg_last_10': None,
                        'avg_last_25': None,
                        'error': 'Failed to fetch player page'
                    })
                    continue

                # Only scrape team if we didn't get it from match link
                if not team:
                    team = scrape_current_team(soup)

                # Scrape match data
                links = scrape_match_links(url)
                if not links:
                    # Get team URL if we have player URL
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
                        'team': team,
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
                    # Get team URL if we have player URL
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
                        'team': team,
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
                    'team': team,
                    'team_url': team_url,
                    'vlr_url': url,
                    'avg_last_5': avgs[5],
                    'avg_last_10': avgs[10],
                    'avg_last_25': avgs[25],
                    'matches_analyzed': len(good_matches)
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
                    'team': None,
                    'team_url': None,
                    'vlr_url': None,
                    'avg_last_5': None,
                    'avg_last_10': None,
                    'avg_last_25': None,
                    'error': error_msg
                })

        # Organize players by match for better frontend display
        # Group players by their teams to identify which matches they're in
        players_by_match = {}  # Map match_key -> {teams: [team1, team2], players: []}
        
        # Group all players by team first
        players_by_team = {}
        for player_result in results:
            team = player_result.get('team', 'Other')
            if team not in players_by_team:
                players_by_team[team] = []
            players_by_team[team].append(player_result)
        
        # If we have match info, organize players by match
        if len(all_matches_info) > 0:
            print(f"[DEBUG] Organizing {len(results)} players into {len(all_matches_info)} matches")
            print(f"[DEBUG] Teams found: {list(players_by_team.keys())}")
            
            # Track which players are already assigned to avoid duplicates across all matches
            all_assigned_player_names = set()
            
            # For each match, assign players to that match
            for match_info in all_matches_info:
                match_teams_list = match_info['teams']
                if len(match_teams_list) >= 2:
                    team1 = match_teams_list[0]
                    team2 = match_teams_list[1]
                    match_key = f"{team1} vs {team2}"
                    
                    print(f"[DEBUG] Processing match: {match_key}")
                    
                    if match_key not in players_by_match:
                        players_by_match[match_key] = {
                            'teams': [team1, team2],
                            'players': []
                        }
                    
                    # Add players from both teams to this match
                    for team, players in players_by_team.items():
                        normalized_team = normalize_name(team)
                        normalized_team1 = normalize_name(team1)
                        normalized_team2 = normalize_name(team2)
                        
                        # More robust matching - check exact match first, then partial
                        team_matches = (
                            normalized_team == normalized_team1 or 
                            normalized_team == normalized_team2
                        )
                        
                        # Also check if team names contain each other (for variations like "MAYHEM" vs "Mayhem")
                        if not team_matches:
                            team_matches = (
                                normalized_team1 in normalized_team or
                                normalized_team in normalized_team1 or
                                normalized_team2 in normalized_team or
                                normalized_team in normalized_team2
                            )
                        
                        if team_matches:
                            print(f"[DEBUG] Matched team '{team}' to match '{match_key}'")
                            # Only add players that haven't been added to any match yet
                            for player in players:
                                player_name = player.get('player', '')
                                if player_name and player_name not in all_assigned_player_names:
                                    players_by_match[match_key]['players'].append(player)
                                    all_assigned_player_names.add(player_name)
                        else:
                            print(f"[DEBUG] Team '{team}' (normalized: '{normalized_team}') did not match '{normalized_team1}' or '{normalized_team2}'")
            
            # Add any remaining players (not in any match) to "Other"
            # Use player names as identifiers since dicts aren't hashable
            all_assigned_player_names = set()
            for match_data in players_by_match.values():
                for player in match_data['players']:
                    player_name = player.get('player', '')
                    if player_name:
                        all_assigned_player_names.add(player_name)
            
            for team, players in players_by_team.items():
                for player in players:
                    player_name = player.get('player', '')
                    if player_name and player_name not in all_assigned_player_names:
                        if 'Other' not in players_by_match:
                            players_by_match['Other'] = {
                                'teams': [],
                                'players': []
                            }
                        players_by_match['Other']['players'].append(player)
                        all_assigned_player_names.add(player_name)  # Mark as assigned
        elif len(match_teams) >= 2:
            # Fallback: use single match teams if we have them
            team1 = match_teams[0]
            team2 = match_teams[1]
            match_key = f"{team1} vs {team2}"
            
            players_by_match[match_key] = {
                'teams': [team1, team2],
                'players': []
            }
            
            # Add players from both teams to this match
            # Track which players are already assigned to avoid duplicates
            assigned_player_names = set()
            for existing_match_data in players_by_match.values():
                for existing_player in existing_match_data['players']:
                    assigned_player_names.add(existing_player.get('player', ''))
            
            for team, players in players_by_team.items():
                normalized_team = normalize_name(team)
                normalized_team1 = normalize_name(team1)
                normalized_team2 = normalize_name(team2)
                
                if (normalized_team == normalized_team1 or 
                    normalized_team == normalized_team2 or
                    normalized_team1 in normalized_team or
                    normalized_team in normalized_team1 or
                    normalized_team2 in normalized_team or
                    normalized_team in normalized_team2):
                    # Only add players not already assigned
                    for player in players:
                        player_name = player.get('player', '')
                        if player_name and player_name not in assigned_player_names:
                            players_by_match[match_key]['players'].append(player)
                            assigned_player_names.add(player_name)
                else:
                    # Player not in this match, add to "Other" category if not already assigned
                    if 'Other' not in players_by_match:
                        players_by_match['Other'] = {
                            'teams': [],
                            'players': []
                        }
                    for player in players:
                        player_name = player.get('player', '')
                        if player_name and player_name not in assigned_player_names:
                            players_by_match['Other']['players'].append(player)
                            assigned_player_names.add(player_name)
        else:
            # No match info, just group by team
            for team, players in players_by_team.items():
                players_by_match[team] = {
                    'teams': [team],
                    'players': players
                }
        
        return jsonify({
            'players': results,
            'match_teams': match_teams,
            'match_url': match_url if use_match_link and match_url else None,
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
                'player': player_name,
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
            'player': player_name,
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

