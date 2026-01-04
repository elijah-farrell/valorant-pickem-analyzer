import requests
from datetime import datetime
from bs4 import BeautifulSoup

BASE_URL = "https://www.vlr.gg"

# Playwright for JavaScript rendering (optional, only if needed)
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    print("[WARN] Playwright not installed. JavaScript-rendered pages won't work.")

def normalize_name(name: str) -> str:
    """Normalize player name for comparison - more flexible matching"""
    if not name:
        return ""
    # Remove common characters and normalize
    normalized = name.lower().strip()
    # Remove spaces, dots, dashes, underscores
    normalized = normalized.replace(" ", "").replace(".", "").replace("-", "").replace("_", "")
    # Remove common prefixes/suffixes
    normalized = normalized.replace("@", "")
    return normalized

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
}

def fetch_soup(url: str, use_playwright=False):
    """Fetch HTML and parse with BeautifulSoup. Optionally use Playwright for JS rendering."""
    if use_playwright and PLAYWRIGHT_AVAILABLE:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(url, wait_until="networkidle", timeout=30000)
                # Wait a bit for dynamic content to load
                page.wait_for_timeout(2000)
                html = page.content()
                browser.close()
                return BeautifulSoup(html, "html.parser")
        except Exception as e:
            print(f"[DEBUG] Playwright failed, falling back to requests: {e}")
            # Fall through to requests
    
    try:
        res = requests.get(url, headers=HEADERS, timeout=15)
        res.raise_for_status()
        return BeautifulSoup(res.text, "html.parser")
    except requests.RequestException as e:
        return None

def find_player_url(player_name: str):
    search_url = f"{BASE_URL}/search/?q={player_name}&type=players"
    res = requests.get(search_url, headers=HEADERS)
    if not res.ok:
        print(f"Failed to search for {player_name}")
        return None

    soup = BeautifulSoup(res.text, "html.parser")
    normalized_query = normalize_name(player_name)
    player_links = soup.select("a.wf-module-item.search-item")

    for tag in player_links:
        player_title_div = tag.select_one("div.search-item-title")
        if not player_title_div:
            continue
        display_name = normalize_name(player_title_div.text)
        if normalized_query in display_name:
            href = tag.get('href', '')
            print(f"[DEBUG] Found player link: {href}")
            # Make sure it's a direct player URL, not a search redirect
            if href.startswith('/player/'):
                return BASE_URL + href
            elif '/player/' in href:
                # Extract the player URL part
                parts = href.split('/player/')
                if len(parts) > 1:
                    return BASE_URL + '/player/' + parts[1]
            return BASE_URL + href

    if player_links:
        href = player_links[0].get('href', '')
        print(f"[DEBUG] Using first result link: {href}")
        if href.startswith('/player/'):
            return BASE_URL + href
        elif '/player/' in href:
            parts = href.split('/player/')
            if len(parts) > 1:
                return BASE_URL + '/player/' + parts[1]
        return BASE_URL + href

    return None

def scrape_current_team(soup: BeautifulSoup):
    team_header = soup.find("h2", string=lambda s: s and "Current Teams" in s)
    if not team_header:
        return None
    wf_card = team_header.find_next_sibling("div", class_="wf-card")
    if not wf_card:
        return None
    team_link = wf_card.find("a", class_="wf-module-item")
    if team_link:
        team_div = team_link.find("div", style=lambda s: s and "font-weight: 500;" in s)
        if team_div:
            return team_div.text.strip()
    return None

def scrape_agent_stats_by_timespan(player_url: str):
    timespans = ["30d", "60d", "90d", "all"]
    stats_by_timespan = {}

    for span in timespans:
        url = f"{player_url}?timespan={span}"
        soup = fetch_soup(url)
        if not soup:
            continue

        table = soup.find("table", class_="wf-table")
        if not table:
            continue

        agent_stats = {}
        for row in table.select("tbody tr"):
            cols = row.find_all("td")
            if len(cols) < 17:
                continue
            try:
                agent_img = cols[0].find("img")
                agent = agent_img["alt"].capitalize() if agent_img and "alt" in agent_img.attrs else "Unknown"
                rounds = int(cols[2].text.strip() or "0")
                rating = float(cols[3].text.strip() or "0")
                acs = float(cols[4].text.strip() or "0")
                kd = float(cols[5].text.strip() or "0")
                adr = float(cols[6].text.strip() or "0")
                kast = cols[7].text.strip()
                kpr = float(cols[8].text.strip() or "0")
                apr = float(cols[9].text.strip() or "0")
                fkpr = float(cols[10].text.strip() or "0")
                fdpr = float(cols[11].text.strip() or "0")
                kills = int(cols[12].text.strip() or "0")
                deaths = int(cols[13].text.strip() or "0")
                assists = int(cols[14].text.strip() or "0")
                fk = int(cols[15].text.strip() or "0")
                fd = int(cols[16].text.strip() or "0")
            except Exception:
                continue

            agent_stats[agent] = {
                "Rounds": rounds,
                "Rating": rating,
                "ACS": acs,
                "K/D": kd,
                "ADR": adr,
                "KAST": kast,
                "KPR": kpr,
                "APR": apr,
                "FKPR": fkpr,
                "FDPR": fdpr,
                "Kills": kills,
                "Deaths": deaths,
                "Assists": assists,
                "FK": fk,
                "FD": fd
            }

        # Calculate Overall
        if agent_stats:
            total = {
                "Rounds": 0, "Rating": [], "ACS": [], "K/D": 0, "ADR": [], "KAST": [],
                "KPR": [], "APR": [], "FKPR": [], "FDPR": [],
                "Kills": 0, "Deaths": 0, "Assists": 0, "FK": 0, "FD": 0
            }
            for agent, stats in agent_stats.items():
                total["Rounds"] += stats["Rounds"]
                total["Kills"] += stats["Kills"]
                total["Deaths"] += stats["Deaths"]
                total["Assists"] += stats["Assists"]
                total["FK"] += stats["FK"]
                total["FD"] += stats["FD"]

                for key in ["Rating", "ACS", "ADR", "KPR", "APR", "FKPR", "FDPR"]:
                    total[key].append(stats[key])

                kast_str = stats["KAST"].replace("%", "")
                try:
                    total["KAST"].append(float(kast_str))
                except:
                    pass

            agent_stats["Overall"] = {
                "Rounds": total["Rounds"],
                "Kills": total["Kills"],
                "Deaths": total["Deaths"],
                "Assists": total["Assists"],
                "FK": total["FK"],
                "FD": total["FD"],
                "K/D": round(total["Kills"] / total["Deaths"], 2) if total["Deaths"] else 0,
                "Rating": round(sum(total["Rating"]) / len(total["Rating"]), 2),
                "ACS": round(sum(total["ACS"]) / len(total["ACS"]), 2),
                "ADR": round(sum(total["ADR"]) / len(total["ADR"]), 2),
                "KAST": f"{round(sum(total['KAST']) / len(total['KAST']))}%" if total["KAST"] else "0%",
                "KPR": round(sum(total["KPR"]) / len(total["KPR"]), 2),
                "APR": round(sum(total["APR"]) / len(total["APR"]), 2),
                "FKPR": round(sum(total["FKPR"]) / len(total["FKPR"]), 2),
                "FDPR": round(sum(total["FDPR"]) / len(total["FDPR"]), 2),
            }

        stats_by_timespan[span] = agent_stats

    return stats_by_timespan

def scrape_match_links(player_url):
    """Scrape match links from player's match history page"""
    match_history_url = player_url.replace("/player/", "/player/matches/")
    print(f"[DEBUG] Fetching match history from: {match_history_url}")
    
    # Try with Playwright first (for JS-rendered content)
    soup = None
    if PLAYWRIGHT_AVAILABLE:
        print(f"[DEBUG] Trying with Playwright (JavaScript rendering)...")
        soup = fetch_soup(match_history_url, use_playwright=True)
    
    # Fallback to regular requests if Playwright failed or not available
    if not soup:
        print(f"[DEBUG] Trying with regular requests...")
        soup = fetch_soup(match_history_url, use_playwright=False)
    
    if not soup:
        print(f"[DEBUG] Failed to fetch match history page: {match_history_url}")
        return []

    print(f"[DEBUG] Successfully fetched match history page")
    match_links = []
    
    # SIMPLEST APPROACH: Find all links that look like match URLs
    # VLR match URLs are: /{numeric-id}/slug or just /{numeric-id}
    # Match IDs are typically 4+ digits
    print(f"[DEBUG] Looking for match links on the page...")
    all_links = soup.select("a[href]")
    print(f"[DEBUG] Found {len(all_links)} total links on page")
    
    for link in all_links:
        href = link.get('href', '')
        if not href:
            continue
        
        # Check if this looks like a match URL
        # VLR format: /{numeric-id}/slug or /{numeric-id}
        if href.startswith('/'):
            # Remove leading slash and check pattern
            path = href.strip('/')
            parts = path.split('/')
            if len(parts) > 0:
                first_part = parts[0]
                # Match IDs are numeric and typically 4+ digits
                if first_part.isdigit() and len(first_part) >= 4:
                    # This is likely a match URL!
                    full_url = BASE_URL + href
                    if full_url not in match_links:
                        match_links.append(full_url)
                        print(f"[DEBUG] Found match link: {full_url}")
        elif href.startswith('http') and 'vlr.gg' in href:
            # Full URL - check if it's a match URL
            # Extract the path part
            try:
                from urllib.parse import urlparse
                parsed = urlparse(href)
                path = parsed.path.strip('/')
                parts = path.split('/')
                if len(parts) > 0:
                    first_part = parts[0]
                    # Check if first part is a numeric match ID
                    if first_part.isdigit() and len(first_part) >= 4:
                        if href not in match_links:
                            match_links.append(href)
                            print(f"[DEBUG] Found match link (full URL): {href}")
            except:
                pass
    
    if match_links:
        print(f"[DEBUG] Found {len(match_links)} match links!")
        return match_links  # Return - we found matches!
    
    # FALLBACK: Try data-match-id approach if no links found
    print(f"[DEBUG] Fallback: Checking data-match-id attributes...")
    data_elements = soup.select("[data-match-id]")
    print(f"[DEBUG] Found {len(data_elements)} elements with data-match-id")
    
    if data_elements:
        for elem in data_elements:
            # Try to find parent link
            parent_link = elem.find_parent('a', href=True)
            if parent_link:
                href = parent_link.get('href', '')
                if href:
                    if href.startswith('/'):
                        match_url = BASE_URL + href
                    elif href.startswith('http'):
                        match_url = href
                    else:
                        continue
                    if match_url not in match_links:
                        match_links.append(match_url)
                        print(f"[DEBUG] Found match link from data-match-id: {match_url}")
        
        if match_links:
            print(f"[DEBUG] Found {len(match_links)} match links from data-match-id!")
            return match_links
    
    # Fallback: Try multiple selectors for match links
    # VLR uses different structures, so we'll try several approaches
    selectors = [
        "a[href*='/match/']",  # Direct match links
        "a.wf-module-item[href*='/']",  # Module items that might be matches
    ]
    
    print(f"[DEBUG] Trying selector-based approach...")
    for selector in selectors:
        links = soup.select(selector)
        print(f"[DEBUG] Selector '{selector}' found {len(links)} links")
        for a in links:
            href = a.get('href')
            if href and '/match/' in href:
                # Clean up the href
                if href.startswith('/'):
                    full_link = BASE_URL + href
                elif href.startswith('http'):
                    full_link = href
                else:
                    continue
                
                # Make sure it's a valid match URL (has numeric ID)
                parts = full_link.split('/')
                if len(parts) >= 4 and parts[-1].isdigit():
                    if full_link not in match_links:
                        match_links.append(full_link)
                        print(f"[DEBUG] Added match link: {full_link}")
    
    # Also try finding links in match history table
    print(f"[DEBUG] Trying table-based approach...")
    match_tables = soup.select("table.wf-table, table.wf-table-inset")
    print(f"[DEBUG] Found {len(match_tables)} tables")
    for table in match_tables:
        rows = table.select("tbody tr")
        print(f"[DEBUG] Found {len(rows)} rows in table")
        for row in rows:
            link_tag = row.select_one("a[href*='/match/']")
            if link_tag:
                href = link_tag.get('href')
                if href:
                    if href.startswith('/'):
                        full_link = BASE_URL + href
                    elif href.startswith('http'):
                        full_link = href
                    else:
                        continue
                    if full_link not in match_links:
                        match_links.append(full_link)
                        print(f"[DEBUG] Added match link from table: {full_link}")
    
    # Try even more generic approach - find ALL links and filter
    print(f"[DEBUG] Trying generic link approach...")
    all_links = soup.select("a[href]")
    print(f"[DEBUG] Found {len(all_links)} total links on page")
    
    # Look for links that contain match IDs we found
    if match_ids:
        print(f"[DEBUG] Looking for links containing match IDs: {match_ids[:5]}...")
        for link in all_links:
            href = link.get('href', '')
            if href:
                # VLR match URLs are /{id}/slug format, check if href contains any match ID
                for mid in match_ids:
                    mid_str = str(mid)
                    # Check if the href contains the match ID (could be /{id} or /{id}/slug)
                    if f'/{mid_str}/' in href or href.endswith(f'/{mid_str}') or href == f'/{mid_str}':
                        if href.startswith('/'):
                            full_link = BASE_URL + href
                        elif href.startswith('http'):
                            full_link = href
                        else:
                            continue
                        if full_link not in match_links:
                            match_links.append(full_link)
                            print(f"[DEBUG] Found match link via ID search: {full_link}")
                            break
    
    # Debug: Show first 10 links to see what we're dealing with
    print(f"[DEBUG] Sample links found:")
    for i, a in enumerate(all_links[:10]):
        href = a.get('href', '')
        text = a.text.strip()[:50] if a.text else ''
        print(f"[DEBUG]   Link {i+1}: {href} (text: {text})")
    
    for a in all_links:
        href = a.get('href', '')
        if '/match/' in href:
            if href.startswith('/'):
                full_link = BASE_URL + href
            elif href.startswith('http'):
                full_link = href
            else:
                continue
            # Check if it's a valid match URL
            parts = full_link.split('/')
            if len(parts) >= 4:
                # Try to extract match ID - could be in different positions
                match_id = None
                for part in parts:
                    if part.isdigit() and len(part) >= 4:  # Match IDs are usually 4+ digits
                        match_id = part
                        break
                if match_id and full_link not in match_links:
                    match_links.append(full_link)
                    print(f"[DEBUG] Added match link (generic): {full_link}")
    
    
    # Check for script tags that might contain match data (JSON/API responses)
    scripts = soup.select("script")
    print(f"[DEBUG] Found {len(scripts)} script tags")
    for i, script in enumerate(scripts):
        if script.string:
            content = script.string
            # Look for JSON data or API endpoints
            if 'match' in content.lower() or 'game' in content.lower() or 'player' in content.lower():
                # Check if it contains JSON-like data
                if '{' in content and ('match' in content.lower() or 'id' in content.lower()):
                    print(f"[DEBUG] Script {i+1} contains potential match data (length: {len(content)})")
                    # Try to find URLs or IDs in the script
                    import re
                    # Look for match URLs
                    match_urls = re.findall(r'["\']([^"\']*match[^"\']*)["\']', content, re.IGNORECASE)
                    if match_urls:
                        print(f"[DEBUG]   Found potential match URLs in script: {match_urls[:5]}")
                    # Look for numeric IDs that might be match IDs
                    match_ids = re.findall(r'\b\d{4,}\b', content)  # 4+ digit numbers
                    if match_ids:
                        print(f"[DEBUG]   Found potential match IDs: {match_ids[:10]}")
    
    # Check page structure - maybe matches are in a different format
    print(f"[DEBUG] Checking page structure...")
    # Look for common match container classes
    match_containers = soup.select("[class*='match'], [class*='game'], [class*='event']")
    print(f"[DEBUG] Found {len(match_containers)} elements with match/game/event in class")
    
    # Check if matches are in data attributes
    for elem in data_elements[:10]:  # Check first 10
        attrs = elem.attrs
        for key, value in attrs.items():
            if 'match' in key.lower() or 'game' in key.lower():
                print(f"[DEBUG]   Found data attribute: {key} = {value}")
    
    print(f"[DEBUG] Total match links found: {len(match_links)}")
    return match_links

def get_match_title(soup):
    team_tags = soup.select("div.match-header-link-name .wf-title-med")
    if len(team_tags) >= 2:
        team1 = team_tags[0].text.strip()
        team2 = team_tags[1].text.strip()
        return f"{team1} vs {team2}"
    return "<Unknown Match>"

def get_match_date(soup):
    """Extract match date from various possible locations"""
    date_selectors = [
        ".match-header-date .moment-tz-convert",
        ".match-header-date",
        "[class*='match-date']",
        ".moment-tz-convert"
    ]
    
    for selector in date_selectors:
        date_tag = soup.select_one(selector)
        if date_tag:
            # Try data attribute first
            raw = date_tag.get("data-utc-ts") or date_tag.get("data-timestamp")
            if raw:
                try:
                    if ":" in raw:
                        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
                        return dt.strftime("%Y-%m-%d")
                    else:
                        dt = datetime.strptime(raw, "%Y-%m-%d")
                        return dt.strftime("%Y-%m-%d")
                except:
                    pass
            
            # Try text content
            date_text = date_tag.text.strip()
            if date_text:
                return date_text.split()[0] if ' ' in date_text else date_text
    
    return "<Unknown Date>"

def parse_match_page(match_url, player_name):
    """Parse a match page to extract player's stats for each map"""
    soup = fetch_soup(match_url)
    if not soup:
        return []

    maps_data = []
    match_title = get_match_title(soup)
    match_date = get_match_date(soup)

    # Try multiple selectors for map sections
    map_sections = soup.select("div.vm-stats-game")
    if not map_sections:
        # Try alternative selector
        map_sections = soup.select("div[class*='stats-game'], div.match-stats-game")
    
    if not map_sections:
        print(f"[DEBUG] No map sections found in {match_url}")
        return []

    normalized_player = normalize_name(player_name)
    print(f"[DEBUG] Looking for player '{player_name}' (normalized: '{normalized_player}') in {match_url}")
    
    for section in map_sections:
        # Try multiple ways to get map name
        map_name = None
        map_selectors = [
            ".vm-stats-game-header .map span",
            ".map span",
            "div.map span",
            ".stats-game-header .map"
        ]
        
        for selector in map_selectors:
            map_label = section.select_one(selector)
            if map_label:
                map_name = " ".join(map_label.text.replace("PICK", "").split()).strip()
                break
        
        if not map_name or map_name.lower() in ("all maps", "<unknown map>", ""):
            continue

        # Try multiple selectors for player rows
        rows = section.select("table.wf-table-inset tbody tr")
        if not rows:
            rows = section.select("table.wf-table tbody tr")
        if not rows:
            rows = section.select("tbody tr")

        for row in rows:
            # Try multiple ways to find player name
            player_cell = None
            name_selectors = [
                "td.mod-player div.text-of",
                "td.mod-player",
                "td[class*='player']",
                "td:first-child"
            ]
            
            for selector in name_selectors:
                player_cell = row.select_one(selector)
                if player_cell:
                    break
            
            if not player_cell:
                continue
            
            # Get player name - try title attribute first, then text
            name = player_cell.get("title") or player_cell.get("data-title") or player_cell.text.strip()
            if not name:
                continue
                
            # More flexible name matching
            normalized_name = normalize_name(name)
            if normalized_name != normalized_player:
                # Try partial match (in case of nicknames or variations)
                if normalized_player not in normalized_name and normalized_name not in normalized_player:
                    continue
                else:
                    print(f"[DEBUG] Partial match found: '{name}' (normalized: '{normalized_name}') matches '{normalized_player}'")

            # Get agent
            agent = "<Unknown Agent>"
            agent_selectors = ["td img", "img[alt]", "td:first-child img"]
            for selector in agent_selectors:
                agent_img = row.select_one(selector)
                if agent_img and agent_img.get("alt"):
                    agent = agent_img["alt"].strip()
                    break

            # Get kills - try multiple selectors
            kills = 0
            kills_selectors = [
                "td.mod-stat.mod-vlr-kills .mod-both",
                "td.mod-stat.mod-vlr-kills",
                "td[class*='kills']",
                "td:nth-child(3)",  # Kills often in 3rd column
            ]
            
            for selector in kills_selectors:
                kills_cell = row.select_one(selector)
                if kills_cell:
                    # Try to find the number
                    both = kills_cell.select_one(".mod-both")
                    if both:
                        try:
                            kills = int(both.text.strip())
                            break
                        except:
                            pass
                    # Try direct text
                    try:
                        kills = int(kills_cell.text.strip())
                        break
                    except:
                        pass

            maps_data.append({
                "map": map_name,
                "agent": agent,
                "kills": kills,
                "match_url": match_url,
                "match_title": match_title,
                "match_date": match_date
            })
            print(f"[DEBUG] Found player '{name}' on map '{map_name}' with {kills} kills")
            break  # only one row per map for player

    if maps_data:
        print(f"[DEBUG] Successfully parsed {len(maps_data)} maps from {match_url}")
    return maps_data

def group_kills_by_match(all_maps, player_name, max_maps=2):
    match_data = {}
    # First, collect per-map stats into each match bucket
    for m in all_maps:
        key = m["match_url"]
        if key not in match_data:
            match_data[key] = {
                "player": player_name,
                "match": m["match_title"],
                "date": m["match_date"],
                "match_url": m["match_url"],
                "map_kills": [],
                "maps_counted": 0
            }
        # Only add up to max_maps
        if match_data[key]["maps_counted"] < max_maps:
            match_data[key]["map_kills"].append({
                "map": m["map"],
                "agent": m["agent"],
                "kills": m["kills"]
            })
            match_data[key]["maps_counted"] += 1

    # Now compute totals, but only include “good” matches
    filtered = []
    for match in match_data.values():
        # sum kills
        total = sum(m["kills"] for m in match["map_kills"])
        match["total_kills"] = total

        # only keep if we got exactly max_maps and >0 kills
        if match["maps_counted"] == max_maps and total > 0:
            filtered.append(match)

    return filtered
