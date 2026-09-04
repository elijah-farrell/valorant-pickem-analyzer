"""Flask API backend for the Valorant pick'em analyzer."""

import os
import sys
import uuid
import json
import logging
import threading
import queue
import time
from datetime import datetime, timedelta

from flask import Flask, jsonify, request, send_from_directory, Response, stream_with_context
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from clients.underdog import get_pickem_slate
from scrapers.vlr import (
    search_player,
    scrape_current_team,
    scrape_player_name,
    scrape_match_links,
    parse_match_page,
    group_kills_by_match,
    fetch_page,
    get_team_url_from_player,
)

def _configure_logging():
    """Stdout logs so Render (and local `python app.py`) show scrape failures."""
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(fmt)
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in ("vlr", "pickem", "underdog"):
        logging.getLogger(name).setLevel(logging.INFO)

_configure_logging()
logger = logging.getLogger("pickem")

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

# Progress tracking - simple in-memory store (cleaned up after 5 minutes)
progress_store = {}
progress_queues = {}  # job_id -> queue.Queue for SSE updates
progress_lock = threading.Lock()

def update_progress(job_id, status, current, total, details=None, result=None):
    """Update progress for a job and push to SSE queue"""
    with progress_lock:
        progress_data = {
            'status': status,  # 'loading', 'complete', 'error'
            'current': current,
            'total': total,
            'details': details or [],
            'updated_at': datetime.now()
        }
        if result is not None:
            progress_data['result'] = result
        else:
            prev = progress_store.get(job_id)
            if prev and prev.get('result') is not None:
                progress_data['result'] = prev['result']
        progress_store[job_id] = progress_data
        
        # Ensure queue exists (create if doesn't exist yet)
        if job_id not in progress_queues:
            progress_queues[job_id] = queue.Queue(maxsize=50)  # Larger queue for more updates
        
        # Push to SSE queue
        try:
            progress_queues[job_id].put_nowait(progress_data)
        except queue.Full:
            # Queue full - try to clear old items and add new one
            try:
                # Remove oldest item
                progress_queues[job_id].get_nowait()
                progress_queues[job_id].put_nowait(progress_data)
            except queue.Empty:
                pass
        
        # Clean up old progress (older than 5 minutes)
        cutoff = datetime.now() - timedelta(minutes=5)
        to_remove = [jid for jid, data in progress_store.items() if data['updated_at'] < cutoff]
        for jid in to_remove:
            if jid in progress_store:
                del progress_store[jid]
            if jid in progress_queues:
                del progress_queues[jid]

def get_progress(job_id):
    """Get current progress for a job"""
    with progress_lock:
        return progress_store.get(job_id)

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

MAX_MATCHES = int(os.environ.get('MAX_MATCHES', '40'))


def progress_to_json(progress_data):
    """Serialize progress for JSON/SSE (no datetime fields)."""
    if not progress_data:
        return None
    data = {
        'status': progress_data['status'],
        'current': progress_data['current'],
        'total': progress_data['total'],
        'details': progress_data['details'],
        'progress_pct': round((progress_data['current'] / progress_data['total']) * 100)
        if progress_data['total'] > 0 else 0,
    }
    if progress_data.get('result') is not None:
        data['result'] = progress_data['result']
    return data

def compute_averages(good_matches, windows=(5, 10, 25)):
    kills = [m['total_kills'] for m in good_matches]
    averages = {}
    for w in windows:
        averages[w] = round(sum(kills[:w]) / w, 2) if len(kills) >= w else None
    return averages


def summarize_slate_diagnostics(results):
    failed = [r for r in results if r.get('error')]
    ok = len(results) - len(failed)
    stages = {}
    for r in failed:
        stage = (r.get('debug') or {}).get('stage') or 'unknown'
        stages[stage] = stages.get(stage, 0) + 1
    sample = next((r for r in failed if (r.get('debug') or {}).get('stage') != 'skipped'), None)
    if sample is None:
        sample = failed[0] if failed else None
    return {
        'players_ok': ok,
        'players_failed': len(failed),
        'failure_stages': stages,
        'sample_error': sample.get('error') if sample else None,
        'sample_debug': sample.get('debug') if sample else None,
    }


def _is_systemic_vlr_failure(row):
    """True when the next players would almost certainly fail the same way."""
    if not row or not row.get('error'):
        return False
    debug = row.get('debug') or {}
    if debug.get('stage') == 'skipped':
        return False
    if debug.get('blocked') or debug.get('block_reason'):
        return True
    if debug.get('status') in (401, 403, 429, 503):
        return True
    if debug.get('reason') in ('markup_changed_ovw', 'fetch_failed', 'no_map_sections'):
        return True
    if debug.get('stage') == 'search' and debug.get('status') == 200 and debug.get('player_result_links') == 0:
        # 0 listed results is a name miss (e.g. Underdog "luk xo" vs VLR "lukxo"), not a site outage.
        if debug.get('listed_result_count') == 0:
            return False
        return True
    if debug.get('fetch_error') and debug.get('stage') in ('search', 'player_page', 'match_history', 'match_parse'):
        return True
    err = (row.get('error') or '').lower()
    if 'layout changed' in err or 'html likely changed' in err or 'parser needs' in err:
        return True
    return False


def _is_player_not_found(row):
    debug = (row or {}).get('debug') or {}
    return debug.get('stage') == 'search' and debug.get('listed_result_count') == 0


def _should_abort_slate(results):
    if not results:
        return False
    last = results[-1]
    if _is_systemic_vlr_failure(last):
        return True
    if len(results) >= 2:
        a, b = results[-2], results[-1]
        if a.get('error') and b.get('error'):
            if _is_player_not_found(a) or _is_player_not_found(b):
                return False
            sa = (a.get('debug') or {}).get('stage')
            sb = (b.get('debug') or {}).get('stage')
            if sa and sa == sb and sa != 'skipped':
                return True
    return False


def _organize_players_by_match(results, all_matches_info, match_id_to_game):
    players_by_match = {}
    if len(all_matches_info) > 0:
        match_id_to_results = {}
        for player_result in results:
            match_id = player_result.get('match_id')
            if match_id:
                if match_id not in match_id_to_results:
                    match_id_to_results[match_id] = []
                match_id_to_results[match_id].append(player_result)
        for match_info in all_matches_info:
            match_id = match_info.get('match_id')
            match_key = match_info.get('match_key')
            teams = match_info.get('teams', [])
            if not match_key or len(teams) < 2:
                continue
            team1, team2 = teams[0], teams[1]
            if match_key not in players_by_match:
                players_by_match[match_key] = {'teams': [team1, team2], 'players': []}
            match_players = match_id_to_results.get(match_id, [])
            team1_players = []
            team2_players = []
            game = match_id_to_game.get(match_id)
            if not game:
                continue
            home_team_id = game.get("home_team_id")
            away_team_id = game.get("away_team_id")
            for player_result in match_players:
                player_team_id = player_result.get('team_id')
                if not player_team_id:
                    continue
                if player_team_id == home_team_id:
                    team1_players.append(player_result)
                elif player_team_id == away_team_id:
                    team2_players.append(player_result)
            players_by_match[match_key]['players'].extend(team1_players)
            players_by_match[match_key]['players'].extend(team2_players)
        all_assigned_player_names = set()
        for match_data in players_by_match.values():
            for p in match_data['players']:
                pname = p.get('player', '').strip()
                if pname:
                    all_assigned_player_names.add(pname)
        for player_result in results:
            pname = player_result.get('player', '').strip()
            if pname and pname not in all_assigned_player_names:
                if 'Other' not in players_by_match:
                    players_by_match['Other'] = {'teams': [], 'players': []}
                players_by_match['Other']['players'].append(player_result)
                all_assigned_player_names.add(pname)
    elif results:
        players_by_match['All Players'] = {
            'teams': [],
            'players': results
        }
    return players_by_match


def _slate_result(results, match_teams, match_url, all_matches_info, match_id_to_game, extra_diagnostics=None):
    diagnostics = summarize_slate_diagnostics(results)
    if extra_diagnostics:
        diagnostics.update(extra_diagnostics)
    return {
        'players': results,
        'match_teams': match_teams,
        'match_url': match_url if match_url else None,
        'players_by_match': _organize_players_by_match(results, all_matches_info, match_id_to_game),
        'diagnostics': diagnostics,
    }


def get_player_vlr_kill_averages(player_name, progress_callback=None):
    """
    Shared VLR pipeline: find player -> fetch page -> scrape match links -> parse matches -> kill averages.
    Returns dict with vlr_url, team_url, avg_last_5/10/25, good_matches, soup (for reuse),
    error (human-readable), and debug (structured) if any.
    Used by both /api/slate and /api/player.
    
    progress_callback: Optional function(message, match_progress) to call with progress updates
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
        'debug': None,
    }

    def fail(stage, error, extra=None):
        debug = {'stage': stage, 'vlr_url': out.get('vlr_url')}
        if extra:
            debug.update(extra)
        out['error'] = error
        out['debug'] = debug
        logger.warning("VLR failed player=%s stage=%s error=%s debug=%s", player_name, stage, error, debug)
        return out
    
    if progress_callback:
        progress_callback('Finding player on VLR.gg...', 0.005)
    url, search_debug = search_player(player_name)
    if not url:
        if search_debug.get('blocked') or search_debug.get('fetch_error'):
            msg = (
                f"VLR search failed for {player_name}: "
                f"{search_debug.get('block_reason') or search_debug.get('fetch_error') or search_debug.get('status')}."
            )
        elif search_debug.get('listed_result_count') == 0:
            queried = search_debug.get('queried_as') or player_name
            msg = f'Player "{player_name}" not found on VLR.gg'
            if queried != player_name:
                msg = f'{msg} (searched as "{queried}")'
        elif search_debug.get('status') == 200 and search_debug.get('player_result_links') == 0:
            msg = (
                f"VLR search returned HTTP 200 but 0 player result links "
                f"(selector {search_debug.get('selector')}). Search HTML may have changed."
            )
        else:
            msg = f'Player "{player_name}" not found on VLR.gg'
        return fail('search', msg, search_debug)
    
    if progress_callback:
        progress_callback('Loading player page...', 0.015)
    soup, page_meta = fetch_page(url)
    out['vlr_url'] = url
    if not page_meta.get('ok') or not soup:
        try:
            out['team_url'] = get_team_url_from_player(url)
        except Exception:
            pass
        return fail(
            'player_page',
            f"Failed to fetch player page ({page_meta.get('error') or page_meta.get('status')}).",
            page_meta,
        )
    out['soup'] = soup
    try:
        out['team_url'] = get_team_url_from_player(url)
    except Exception:
        pass
    
    if progress_callback:
        progress_callback('Scraping match history...', 0.03)
    hist_debug = {}
    links = scrape_match_links(url, diagnostics=hist_debug)
    if not links:
        out['links_found'] = 0
        out['all_maps_count'] = 0
        return fail(
            'match_history',
            hist_debug.get('hint') or 'No match history found',
            hist_debug,
        )
    
    links_to_check = links[:MAX_MATCHES]
    total_matches = len(links_to_check)
    
    if progress_callback:
        progress_callback(f'Analyzing {total_matches} recent matches...', 0.05)
    all_maps = []
    first_parse_meta = None
    parse_exceptions = 0
    for idx, link in enumerate(links_to_check, 1):
        if progress_callback:
            # Parsing is ~95% of the time; map match progress into 0.05 -> 0.95
            if total_matches:
                match_fraction = idx / total_matches
                match_progress = 0.05 + (0.90 * match_fraction)
            else:
                match_progress = 0.05
            progress_callback(f'Parsing match {idx}/{total_matches}...', match_progress)
        parse_meta = {}
        try:
            maps = parse_match_page(link, player_name, meta_out=parse_meta)
            all_maps.extend(maps)
        except Exception:
            parse_exceptions += 1
            logger.exception("VLR parse_match_page crashed player=%s url=%s", player_name, link)
            continue
        if first_parse_meta is None:
            first_parse_meta = parse_meta
        # Don't grind through all 40 pages if VLR is blocked or the layout is gone.
        if not all_maps and idx >= 2:
            reason = (first_parse_meta or {}).get('reason')
            same_reason = parse_meta.get('reason') == reason
            no_stats_ui = (
                (first_parse_meta or {}).get('ovw_rows', 1) == 0
                and (first_parse_meta or {}).get('tables', 1) == 0
                and parse_meta.get('ovw_rows', 1) == 0
                and parse_meta.get('tables', 1) == 0
            )
            if (reason in ('fetch_failed', 'no_map_sections', 'markup_changed_ovw') and same_reason) or no_stats_ui:
                logger.warning(
                    "Stopping match parse early player=%s after %s pages reason=%s",
                    player_name, idx, reason,
                )
                break
    
    out['links_found'] = len(links)
    out['all_maps_count'] = len(all_maps)
    
    if progress_callback:
        progress_callback('Calculating averages...', 0.99)
    good_matches = group_kills_by_match(all_maps, player_name, max_maps=2)
    if not good_matches:
        reason = (first_parse_meta or {}).get('reason')
        debug = {
            **(first_parse_meta or {}),
            'links_found': len(links),
            'links_checked': len(links_to_check),
            'all_maps_count': len(all_maps),
            'parse_exceptions': parse_exceptions,
        }
        if reason == 'markup_changed_ovw':
            msg = (
                "VLR match page layout changed: player stats are in div.ovw-row, "
                "not table/td.mod-player. Parser needs an update."
            )
            debug['hint'] = (
                f"Sample match had {debug.get('ovw_rows')} ovw-row and "
                f"{debug.get('tables')} tables (title={debug.get('page_title')!r})."
            )
        elif reason == 'fetch_failed':
            msg = (
                f"Failed to fetch VLR match pages "
                f"({(first_parse_meta or {}).get('fetch_error') or (first_parse_meta or {}).get('status')})."
            )
        elif not all_maps:
            msg = (
                f"Opened {len(links_to_check)} VLR match pages but extracted 0 map stats for {player_name}. "
                "Name matching or stats selectors may be wrong."
            )
        else:
            msg = (
                f"Found {len(all_maps)} maps across matches but none had exactly 2 maps with kills."
            )
        return fail('match_parse', msg, debug)
    
    avgs = compute_averages(good_matches)
    out['avg_last_5'] = avgs[5]
    out['avg_last_10'] = avgs[10]
    out['avg_last_25'] = avgs[25]
    out['good_matches'] = good_matches
    out['debug'] = {
        'stage': 'ok',
        'links_found': len(links),
        'links_checked': len(links_to_check),
        'all_maps_count': len(all_maps),
        'good_matches': len(good_matches),
    }
    logger.info(
        "VLR ok player=%s avg5=%s avg10=%s avg25=%s matches=%s",
        player_name, avgs[5], avgs[10], avgs[25], len(good_matches),
    )
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
        except Exception:
            return jsonify({'error': 'Not found'}), 404

@app.route('/health')
@limiter.exempt  # Health check shouldn't be rate limited
def health():
    """Health check endpoint for Render to keep service alive"""
    return jsonify({'status': 'ok', 'service': 'valorant-pickem-analyzer'})

@app.route('/api/progress/<job_id>')
@limiter.exempt  # SSE streaming needs to be exempt from rate limiting
def stream_progress(job_id):
    """Stream progress updates via Server-Sent Events (SSE) - real-time updates"""
    def generate():
        # Get or create queue for this connection
        with progress_lock:
            if job_id not in progress_queues:
                progress_queues[job_id] = queue.Queue(maxsize=50)
            q = progress_queues[job_id]
        
        # Send initial progress if available
        initial = get_progress(job_id)
        if initial:
            yield f"data: {json.dumps(progress_to_json(initial))}\n\n"
        
        # Stream updates from queue
        try:
            while True:
                try:
                    # Wait for update with timeout
                    progress_data = q.get(timeout=30)
                    yield f"data: {json.dumps(progress_to_json(progress_data))}\n\n"
                    
                    # Close connection if complete or error
                    if progress_data['status'] in ('complete', 'error'):
                        break
                        
                except queue.Empty:
                    # Send keepalive to keep connection alive
                    yield ": keepalive\n\n"
                    time.sleep(1)  # Wait 1 second before checking queue again
        finally:
            # Clean up queue when connection closes
            with progress_lock:
                if job_id in progress_queues:
                    try:
                        # Drain queue
                        while True:
                            progress_queues[job_id].get_nowait()
                    except queue.Empty:
                        pass
    
    return Response(stream_with_context(generate()), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no'  # Disable nginx buffering
    })


@app.route('/api/progress/<job_id>/status', methods=['GET'])
@limiter.exempt
def progress_status(job_id):
    """Poll job progress (short requests — works on Render; avoids long-lived SSE timeouts)."""
    progress = get_progress(job_id)
    if not progress:
        return jsonify({
            'status': 'unknown',
            'error': 'Job not found. The server may have restarted during processing.',
        }), 404
    return jsonify(progress_to_json(progress))


def _parse_slate_response(slate_response):
    """Parse Underdog slate response into player_info and match structures. Runs in background thread."""
    over_under_lines = slate_response.get("over_under_lines", [])
    appearances = slate_response.get("appearances", [])
    players_data = slate_response.get("players", [])
    games = slate_response.get("games", [])

    # Map: player_id -> team_id
    player_to_team_id = {}
    for appearance in appearances:
        player_id = appearance.get("player_id")
        team_id = appearance.get("team_id")
        if player_id and team_id:
            player_to_team_id[player_id] = team_id

    # Map: player_id -> player name (last_name) and player_id -> team_id
    player_id_to_name = {}
    player_id_to_team_id_direct = {}
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
        full_title = game.get("full_team_names_title", "")
        if " vs " in full_title:
            parts = full_title.split(" vs ")
            if len(parts) >= 2:
                if home_team_id:
                    team_id_to_name[home_team_id] = parts[0].strip()
                if away_team_id:
                    team_id_to_name[away_team_id] = parts[1].strip()
        elif " vs " in game.get("title", ""):
            parts = game.get("title", "").split(" vs ")
            if len(parts) >= 2:
                if home_team_id:
                    team_id_to_name[home_team_id] = parts[0].strip()
                if away_team_id:
                    team_id_to_name[away_team_id] = parts[1].strip()
        elif " vs " in game.get("short_title", ""):
            parts = game.get("short_title", "").split(" vs ")
            if len(parts) >= 2:
                if home_team_id:
                    team_id_to_name[home_team_id] = parts[0].strip()
                if away_team_id:
                    team_id_to_name[away_team_id] = parts[1].strip()

    # Map: match_id -> game
    match_id_to_game = {}
    for game in games:
        gid = game.get("id")
        if gid:
            match_id_to_game[gid] = game

    # Extract player info from over_under_lines
    player_info = []
    for item in over_under_lines:
        try:
            if not item.get("over_under") or not item["over_under"].get("title"):
                continue
            if "Kills on Maps 1+2 O/U" not in item["over_under"]["title"]:
                continue
            player = item["over_under"]["title"].replace(" Kills on Maps 1+2 O/U", "").strip()
            line = item.get('stat_value')
            options = item.get("options", [])
            odds_over = options[0].get("american_price", "N/A") if len(options) >= 1 else "N/A"
            odds_under = options[1].get("american_price", "N/A") if len(options) >= 2 else "N/A"
            player_normalized = player.strip()
            player_id = None
            team_id = None
            team = None
            match_id = None
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
                game_obj = match_id_to_game.get(match_id) if match_id else None
                if game_obj and appearance_team_id:
                    home_id = game_obj.get("home_team_id")
                    away_id = game_obj.get("away_team_id")
                    if appearance_team_id == home_id:
                        team_id = away_id
                        team = team_id_to_name.get(away_id)
                    elif appearance_team_id == away_id:
                        team_id = home_id
                        team = team_id_to_name.get(home_id)
            if not player_id:
                for pid, pname in player_id_to_name.items():
                    if pname.strip() == player_normalized or pname.strip().lower() == player_normalized.lower():
                        player_id = pid
                        break
            if not team_id and player_id and player_id in player_id_to_team_id_direct:
                team_id = player_id_to_team_id_direct[player_id]
                team = team_id_to_name.get(team_id)
            if not team_id and appearance and appearance.get("team_id"):
                team_id = appearance.get("team_id")
                team = team_id_to_name.get(team_id)
            if not team_id or not team:
                continue
            player_info.append({
                'player': player,
                'line': line,
                'odds_over': odds_over,
                'odds_under': odds_under,
                'team': team,
                'team_id': team_id,
                'player_id': player_id,
                'match_id': match_id
            })
        except Exception:
            continue

    match_teams = []
    all_matches_info = []
    if len(player_info) > 0 and len(games) > 0:
        for game in games:
            match_id = game.get("id")
            home_team_id = game.get("home_team_id")
            away_team_id = game.get("away_team_id")
            home_team_name = team_id_to_name.get(home_team_id)
            away_team_name = team_id_to_name.get(away_team_id)
            if match_id and home_team_name and away_team_name:
                match_key = f"{home_team_name} vs {away_team_name}"
                all_matches_info.append({
                    'url': None,
                    'teams': [home_team_name, away_team_name],
                    'match_key': match_key,
                    'match_id': match_id
                })
                if not match_teams:
                    match_teams = [home_team_name, away_team_name]

    return player_info, match_teams, all_matches_info, match_id_to_game, team_id_to_name, games


@app.route('/api/slate', methods=['GET'])
@limiter.limit("10 per minute")  # Limit to 10 requests per minute per IP
def get_slate():
    """Get Underdog pick'em slate with VLR stats comparison. Returns job_id immediately; parsing and VLR fetch run in background."""
    try:
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

        job_id = str(uuid.uuid4())
        update_progress(job_id, 'loading', 0, 1, ['Parsing slate...'])
        thread = threading.Thread(
            target=_process_slate_background,
            args=(job_id, slate_response, match_url),
            daemon=True
        )
        thread.start()
        return jsonify({'job_id': job_id})
    except Exception:
        error_msg = "Unable to fetch slate data. Please try again later."
        return jsonify({'error': error_msg}), 500


def _process_slate_background(job_id, slate_response, match_url):
    """Parse slate and fetch VLR stats in background. Keeps request thread fast for production timeouts."""
    try:
        update_progress(job_id, 'loading', 0, 1, ['Parsing Underdog slate...'])
        parsed = _parse_slate_response(slate_response)
        player_info, match_teams, all_matches_info, match_id_to_game, _, _ = parsed
        total_players = len(player_info)
        if total_players == 0:
            update_progress(job_id, 'complete', 0, 1, ['No players in slate.'], {
                'players': [],
                'match_teams': [],
                'match_url': match_url,
                'players_by_match': {}
            })
            return
        update_progress(job_id, 'loading', 0, total_players, ['Starting to fetch player stats from VLR.gg...'])
        results = []
        abort_info = None
        for idx, player_data in enumerate(player_info, 1):
            player = player_data['player']
            line = player_data['line']
            team_from_underdog = player_data.get('team')
            team_id_from_underdog = player_data.get('team_id')
            player_id_from_underdog = player_data.get('player_id')

            def make_progress_callback(player_name, player_idx):
                def progress_callback(message, match_progress):
                    completed = player_idx - 1
                    if isinstance(match_progress, (int, float)):
                        current = completed + max(0.0, min(1.0, float(match_progress)))
                    else:
                        current = completed
                    update_progress(job_id, 'loading', current, total_players, [
                        f'Processing {player_name} ({player_idx}/{total_players})... {message}'
                    ])
                return progress_callback

            update_progress(job_id, 'loading', idx - 1, total_players, [
                f'Processing {player} ({idx}/{total_players})...'
            ])
            try:
                r = get_player_vlr_kill_averages(player, progress_callback=make_progress_callback(player, idx))
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
                    'debug': r.get('debug'),
                }
                if r['error']:
                    row['error'] = r['error']
                else:
                    row['matches_analyzed'] = len(r['good_matches'])
                results.append(row)
                detail = (
                    f'{player} failed: {row["error"]}'
                    if row.get('error')
                    else f'Completed {player} ({idx}/{total_players})'
                )
                extra = None
                remaining = player_info[idx:]
                if remaining and _should_abort_slate(results):
                    skip_err = (
                        f'Skipped: stopped after a site-wide VLR failure on {player}. '
                        f'{row.get("error") or "see first failure"}'
                    )
                    logger.warning(
                        "Aborting slate early after %s/%s: %s",
                        idx, total_players, row.get('error'),
                    )
                    for rest in remaining:
                        results.append({
                            'player': rest['player'],
                            'line': rest['line'],
                            'odds_over': rest['odds_over'],
                            'odds_under': rest['odds_under'],
                            'team': rest.get('team'),
                            'team_id': rest.get('team_id'),
                            'player_id': rest.get('player_id'),
                            'team_url': None,
                            'vlr_url': None,
                            'avg_last_5': None,
                            'avg_last_10': None,
                            'avg_last_25': None,
                            'error': skip_err,
                            'debug': {'stage': 'skipped', 'stopped_after': player},
                            'match_id': rest.get('match_id'),
                        })
                    extra = {
                        'aborted_early': True,
                        'skipped': len(remaining),
                        'stopped_after': idx,
                    }
                    abort_info = extra
                    detail = (
                        f'Stopped early after {player} ({idx}/{total_players}): '
                        f'{row.get("error")}. Skipping {len(remaining)} remaining players.'
                    )
                payload = _slate_result(
                    results, match_teams, match_url, all_matches_info, match_id_to_game, extra
                )
                update_progress(
                    job_id, 'loading', idx if not extra else total_players, total_players,
                    [detail], payload,
                )
                if extra:
                    break
            except Exception:
                logger.exception("Error fetching player stats for %s", player)
                error_msg = "Error fetching player stats (see server logs)"
                results.append({
                    'player': player,
                    'line': line,
                    'odds_over': player_data['odds_over'],
                    'odds_under': player_data['odds_under'],
                    'team': team_from_underdog,
                    'team_id': team_id_from_underdog,
                    'player_id': player_id_from_underdog,
                    'team_url': None,
                    'vlr_url': None,
                    'avg_last_5': None,
                    'avg_last_10': None,
                    'avg_last_25': None,
                    'error': error_msg,
                    'debug': {'stage': 'exception'},
                    'match_id': player_data.get('match_id')
                })
                update_progress(
                    job_id, 'loading', idx, total_players,
                    [f'{player} failed: {error_msg}'],
                    _slate_result(results, match_teams, match_url, all_matches_info, match_id_to_game),
                )

        final = _slate_result(
            results, match_teams, match_url, all_matches_info, match_id_to_game, abort_info
        )
        diagnostics = final['diagnostics']
        if diagnostics['players_failed']:
            logger.warning(
                "Slate complete ok=%s failed=%s stages=%s sample_error=%s aborted=%s",
                diagnostics['players_ok'],
                diagnostics['players_failed'],
                diagnostics['failure_stages'],
                diagnostics['sample_error'],
                diagnostics.get('aborted_early'),
            )
        else:
            logger.info("Slate complete ok=%s failed=0", diagnostics['players_ok'])
        done_msg = 'Complete! All players processed.'
        if diagnostics.get('aborted_early'):
            done_msg = (
                f"Stopped early after {diagnostics.get('stopped_after')} player(s). "
                f"{diagnostics.get('sample_error') or ''}"
            ).strip()
        update_progress(
            job_id, 'complete', len(results), total_players, [done_msg], final
        )
    except Exception as e:
        logger.exception("Slate job failed job_id=%s", job_id)
        total = total_players if 'total_players' in locals() else 1
        update_progress(job_id, 'error', 0, total, [f'Error: {str(e)}'])

@app.route('/api/player/<player_name>', methods=['GET'])
@limiter.limit("20 per minute")  # Limit to 20 requests per minute per IP
def get_player_stats(player_name):
    """Get detailed stats for a specific player. Uses shared get_player_vlr_kill_averages + VLR-only extras (name, team)."""
    try:
        r = get_player_vlr_kill_averages(player_name)
        debug_info = r.get('debug') or {}
        debug_info.update({
            'links_found': r.get('links_found', 0),
            'links_checked': min(r.get('links_found', 0), MAX_MATCHES),
            'all_maps_count': r.get('all_maps_count', 0),
            'good_matches_count': len(r.get('good_matches') or []),
        })

        if r['error'] and not r['vlr_url']:
            return jsonify({'error': r['error'], 'debug': debug_info}), 404
        if r['error'] and (r.get('debug') or {}).get('stage') == 'player_page':
            return jsonify({'error': r['error'], 'debug': debug_info}), 502

        actual_player_name = (scrape_player_name(r['soup']) or player_name) if r['soup'] else player_name
        team = scrape_current_team(r['soup']) if r['soup'] else None
        team_url = r['team_url']
        vlr_url = r['vlr_url']
        good_matches = r['good_matches']
        all_maps_count = r.get('all_maps_count', 0)

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
    except Exception:
        logger.exception("Player stats request failed for %s", player_name)
        return jsonify({'error': 'Unable to fetch player stats. Please try again later.'}), 500

def _is_production_runtime():
    """True on Render/production hosts. Local `python app.py` stays in dev mode."""
    if os.environ.get('FLASK_ENV', '').lower() == 'production':
        return True
    if os.environ.get('RENDER', '').lower() in ('true', '1', 'yes'):
        return True
    return bool(os.environ.get('ALLOWED_ORIGINS', '').strip())


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    prod = _is_production_runtime()
    # Production on Render must use gunicorn (set in Render dashboard). This block is local dev only.
    app.run(debug=not prod, use_reloader=not prod, host='0.0.0.0', port=port)

