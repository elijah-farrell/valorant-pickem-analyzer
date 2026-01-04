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
        slate = get_pickem_slate()
        if not slate:
            return jsonify({'error': 'No pick\'em slate found. Underdog API may be down or no lines available.'}), 500

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

        # Fetch VLR stats for each player
        results = []
        for player_data in player_info:
            player = player_data['player']
            line = player_data['line']
            
            try:
                url = find_player_url(player)
                if not url:
                    results.append({
                        'player': player,
                        'line': line,
                        'odds_over': player_data['odds_over'],
                        'odds_under': player_data['odds_under'],
                        'team': None,
                        'vlr_url': None,
                        'avg_last_5': None,
                        'avg_last_10': None,
                        'avg_last_25': None,
                        'error': 'Player not found on VLR.gg'
                    })
                    continue

                soup = fetch_soup(url)
                if not soup:
                    results.append({
                        'player': player,
                        'line': line,
                        'odds_over': player_data['odds_over'],
                        'odds_under': player_data['odds_under'],
                        'team': None,
                        'vlr_url': url,
                        'avg_last_5': None,
                        'avg_last_10': None,
                        'avg_last_25': None,
                        'error': 'Failed to fetch player page'
                    })
                    continue

                team = scrape_current_team(soup)

                # Scrape match data
                links = scrape_match_links(url)
                if not links:
                    results.append({
                        'player': player,
                        'line': line,
                        'odds_over': player_data['odds_over'],
                        'odds_under': player_data['odds_under'],
                        'team': team,
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
                        'team': team,
                        'vlr_url': url,
                        'avg_last_5': None,
                        'avg_last_10': None,
                        'avg_last_25': None,
                        'error': 'No valid matches found (need matches with exactly 2 maps)'
                    })
                    continue

                avgs = compute_averages(good_matches)
                
                results.append({
                    'player': player,
                    'line': line,
                    'odds_over': player_data['odds_over'],
                    'odds_under': player_data['odds_under'],
                    'team': team,
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
                    'vlr_url': None,
                    'avg_last_5': None,
                    'avg_last_10': None,
                    'avg_last_25': None,
                    'error': error_msg
                })

        return jsonify({'players': results})
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

