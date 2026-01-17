from datetime import datetime

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.vlr.gg"

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

def fetch_soup(url: str):
    """Fetch HTML and parse with BeautifulSoup."""
    try:
        res = requests.get(url, headers=HEADERS, timeout=15)
        res.raise_for_status()
        return BeautifulSoup(res.text, "html.parser")
    except requests.RequestException:
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
        if href.startswith('/player/'):
            return BASE_URL + href
        elif '/player/' in href:
            parts = href.split('/player/')
            if len(parts) > 1:
                return BASE_URL + '/player/' + parts[1]
        return BASE_URL + href

    return None

def scrape_player_name(soup: BeautifulSoup):
    """Extract the actual player display name from VLR player profile page"""
    # Try multiple selectors for player name on profile page
    name_selectors = [
        "h1.wf-title",
        "h1",
        ".player-header h1",
        "div.player-header h1",
        "h1.player-name",
    ]
    
    for selector in name_selectors:
        name_elem = soup.select_one(selector)
        if name_elem:
            name = name_elem.get_text(strip=True)
            if name:
                return name
    
    # Fallback: try to find in page title or meta
    title_tag = soup.find("title")
    if title_tag:
        title_text = title_tag.get_text(strip=True)
        # VLR titles often have format "Player Name - VLR.gg"
        if " - " in title_text:
            return title_text.split(" - ")[0].strip()
        return title_text
    
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

def get_team_url_from_player(player_url):
    """Get the team's VLR page URL from a player's page"""
    soup = fetch_soup(player_url)
    if not soup:
        return None
    
    team_header = soup.find("h2", string=lambda s: s and "Current Teams" in s)
    if not team_header:
        return None
    
    wf_card = team_header.find_next_sibling("div", class_="wf-card")
    if not wf_card:
        return None
    
    team_link = wf_card.find("a", class_="wf-module-item", href=True)
    if team_link:
        href = team_link.get('href', '')
        if href:
            if href.startswith('/team/'):
                return BASE_URL + href
            elif href.startswith('http'):
                return href
            elif '/team/' in href:
                return BASE_URL + href
    return None

def get_match_from_team(team_url):
    """Get the next match URL from a team's VLR page (the next game they play)"""
    soup = fetch_soup(team_url)
    if not soup:
        return []
    
    match_urls = []
    # Look for upcoming matches section on team page
    # VLR team pages have upcoming matches listed first
    # Check for "upcoming" or "next" match sections
    upcoming_sections = soup.select(".wf-module, .upcoming-matches, [class*='upcoming'], [class*='next']")
    
    # Also check all links for match URLs, but prioritize upcoming sections
    all_links = soup.select("a[href]")
    for link in all_links:
        href = link.get('href', '')
        if not href:
            continue
        
        # Check if it's a match URL (numeric ID)
        if href.startswith('/'):
            path = href.strip('/')
            parts = path.split('/')
            if len(parts) > 0 and parts[0].isdigit() and len(parts[0]) >= 4:
                full_url = BASE_URL + href
                if full_url not in match_urls:
                    match_urls.append(full_url)
        elif href.startswith('http') and 'vlr.gg' in href:
            # Check if it's a match URL
            try:
                from urllib.parse import urlparse
                parsed = urlparse(href)
                path = parsed.path.strip('/')
                parts = path.split('/')
                if len(parts) > 0 and parts[0].isdigit() and len(parts[0]) >= 4:
                    if href not in match_urls:
                        match_urls.append(href)
            except:
                pass
    
    # Filter for only upcoming matches and return just the first one
    upcoming_matches = []
    now = datetime.now()
    
    for match_url in match_urls:
        try:
            # Check if match is upcoming by fetching the match page
            match_soup = fetch_soup(match_url)
            if not match_soup:
                continue
            
            # Get match date
            match_date_str = get_match_date(match_soup)
            if match_date_str == "<Unknown Date>":
                # If we can't get date, assume it might be upcoming and check later
                upcoming_matches.append(match_url)
                continue
            
            # Try to parse date and check if it's in the future
            try:
                # Try different date formats
                if ":" in match_date_str or len(match_date_str) > 10:
                    # Has time component or is longer format
                    match_date = datetime.strptime(match_date_str.split()[0], "%Y-%m-%d")
                else:
                    match_date = datetime.strptime(match_date_str, "%Y-%m-%d")
                
                # If match date is today or in the future, it's upcoming
                if match_date >= now.replace(hour=0, minute=0, second=0, microsecond=0):
                    upcoming_matches.append(match_url)
                    # Return the first upcoming match found (the next game)
                    return [match_url]
            except:
                # If date parsing fails, check if match page shows it's upcoming
                # Look for indicators like "TBD", "Upcoming", or no completed stats
                match_title = get_match_title(match_soup)
                # If match has no completed stats, it's likely upcoming
                stats_tables = match_soup.select("table.wf-table-inset, table.wf-table")
                if not stats_tables or len(stats_tables) == 0:
                    # No stats = likely upcoming match
                    return [match_url]
        except Exception as e:
            continue
    
    # If we found upcoming matches, return the first one
    if upcoming_matches:
        return [upcoming_matches[0]]
    
    # If no upcoming matches found but we have matches, return first one (might be upcoming)
    if match_urls:
        return [match_urls[0]]
    
    return []

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
    soup = fetch_soup(match_history_url)
    
    if not soup:
        return []
    match_links = []
    match_ids = []  # Initialize to avoid used-before-assignment error
    
    # SIMPLEST APPROACH: Find all links that look like match URLs
    # VLR match URLs are: /{numeric-id}/slug or just /{numeric-id}
    # Match IDs are typically 4+ digits
    all_links = soup.select("a[href]")
    
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
            except:
                pass
    
    if match_links:
        return match_links  # Return - we found matches!
    
    # FALLBACK: Try data-match-id approach if no links found
    data_elements = soup.select("[data-match-id]")
    
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
        
        if match_links:
            return match_links
    
    # Fallback: Try multiple selectors for match links
    # VLR uses different structures, so we'll try several approaches
    selectors = [
        "a[href*='/match/']",  # Direct match links
        "a.wf-module-item[href*='/']",  # Module items that might be matches
    ]
    
    for selector in selectors:
        links = soup.select(selector)
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
    
    # Also try finding links in match history table
    match_tables = soup.select("table.wf-table, table.wf-table-inset")
    for table in match_tables:
        rows = table.select("tbody tr")
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
    
    # Try even more generic approach - find ALL links and filter
    all_links = soup.select("a[href]")
    
    # Look for links that contain match IDs we found
    if match_ids:
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
                            break
    
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
    
    
    # Check for script tags that might contain match data (JSON/API responses)
    scripts = soup.select("script")
    for i, script in enumerate(scripts):
        if script.string:
            content = script.string
            # Look for JSON data or API endpoints
            if 'match' in content.lower() or 'game' in content.lower() or 'player' in content.lower():
                # Check if it contains JSON-like data
                if '{' in content and ('match' in content.lower() or 'id' in content.lower()):
                    # Try to find URLs or IDs in the script
                    import re
                    # Look for match URLs
                    match_urls = re.findall(r'["\']([^"\']*match[^"\']*)["\']', content, re.IGNORECASE)
                    if match_urls:
                        pass  # Could extract URLs here if needed
                    # Look for numeric IDs that might be match IDs
                    match_ids = re.findall(r'\b\d{4,}\b', content)  # 4+ digit numbers
                    if match_ids:
                        pass  # Could use IDs here if needed
    
    # Check page structure - maybe matches are in a different format
    # Look for common match container classes
    match_containers = soup.select("[class*='match'], [class*='game'], [class*='event']")
    
    # Check if matches are in data attributes
    for elem in data_elements[:10]:  # Check first 10
        attrs = elem.attrs
        for key, value in attrs.items():
            if 'match' in key.lower() or 'game' in key.lower():
                pass  # Could extract match data here if needed
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

def get_match_teams(soup):
    """Extract team names from a match page"""
    teams = []
    team_tags = soup.select("div.match-header-link-name .wf-title-med")
    if len(team_tags) >= 2:
        teams = [team_tags[0].text.strip(), team_tags[1].text.strip()]
    return teams

def find_matches_between_teams(team1, team2, limit=5):
    """Search VLR for matches between two teams (upcoming or recent)"""
    # VLR search for matches - try searching by team names
    # This is a simplified approach - VLR doesn't have a direct API for this
    # We'll search for upcoming matches and filter by team names
    
    # Try searching for upcoming matches page
    upcoming_url = f"{BASE_URL}/matches"
    soup = fetch_soup(upcoming_url)
    if not soup:
        return []
    
    match_links = []
    # Find match links that contain both team names
    normalized_team1 = normalize_name(team1)
    normalized_team2 = normalize_name(team2)
    
    # Look for match links
    links = soup.select("a[href*='/']")
    for link in links:
        href = link.get('href', '')
        if not href:
            continue
        
        # Check if it's a match URL (numeric ID)
        if href.startswith('/'):
            path = href.strip('/')
            parts = path.split('/')
            if len(parts) > 0 and parts[0].isdigit() and len(parts[0]) >= 4:
                # Check if match title/link text contains team names
                link_text = normalize_name(link.text or '')
                parent_text = normalize_name(link.find_parent().text if link.find_parent() else '')
                
                if (normalized_team1 in link_text or normalized_team1 in parent_text or
                    normalized_team2 in link_text or normalized_team2 in parent_text):
                    full_url = BASE_URL + href
                    if full_url not in match_links:
                        match_links.append(full_url)
                        if len(match_links) >= limit:
                            break
    
    return match_links

def find_match_urls_for_teams(team_list, max_matches_per_pair=3):
    """Find VLR match URLs for a list of teams (assumes teams are playing each other)"""
    match_urls = []
    match_teams_map = {}  # Map match_url -> [team1, team2]
    
    # If we have 2+ teams, try to find matches between them
    # Group teams into pairs (most common: 2 teams playing each other)
    if len(team_list) >= 2:
        # Try all pairs
        for i in range(len(team_list)):
            for j in range(i + 1, len(team_list)):
                team1 = team_list[i]
                team2 = team_list[j]
                matches = find_matches_between_teams(team1, team2, limit=max_matches_per_pair)
                for match_url in matches:
                    if match_url not in match_urls:
                        match_urls.append(match_url)
                        match_teams_map[match_url] = [team1, team2]
    
    return match_urls, match_teams_map

def extract_player_links_from_match(match_url):
    """Extract all player profile links from a VLR match page"""
    soup = fetch_soup(match_url)
    if not soup:
        return {}
    
    player_links = {}
    teams = get_match_teams(soup)
    
    # Find all player links in the match stats tables
    # VLR match pages have player links in table rows
    # Try multiple selectors for player rows
    player_rows = soup.select("table.wf-table-inset tbody tr, table.wf-table tbody tr")
    
    for row in player_rows:
        # Find player link or player cell
        player_cell = row.select_one("td.mod-player, td[class*='player']")
        if not player_cell:
            continue
        
        # Try to find link in the cell
        link = player_cell.select_one("a[href*='/player/']")
        if not link:
            continue
        
        href = link.get('href', '')
        if not href or '/player/' not in href:
            continue
        
        # Get player name - try multiple methods
        player_name = (
            link.get('title', '') or 
            link.get('data-title', '') or
            link.text.strip() or
            player_cell.get('title', '') or
            player_cell.text.strip()
        )
        
        # Try to get from text-of div
        if not player_name:
            text_of = player_cell.select_one("div.text-of, .text-of")
            if text_of:
                player_name = text_of.get('title', '') or text_of.text.strip()
        
        if not player_name:
            continue
        
        # Normalize the URL
        if href.startswith('/player/'):
            full_url = BASE_URL + href
        elif href.startswith('http'):
            full_url = href
        else:
            continue
        
        # Store with normalized name as key
        normalized_name = normalize_name(player_name)
        if normalized_name and normalized_name not in player_links:
            player_links[normalized_name] = {
                'url': full_url,
                'display_name': player_name,
                'team': None  # Will be determined later
            }
    
    # Try to determine which team each player belongs to
    # Look at the match stats sections
    map_sections = soup.select("div.vm-stats-game")
    if not map_sections:
        map_sections = soup.select("div[class*='stats-game'], div.match-stats-game")
    
    if map_sections and len(teams) >= 2:
        team1_players = set()
        team2_players = set()
        
        for section in map_sections:
            # Find team sections (usually two tables or two groups)
            tables = section.select("table.wf-table-inset, table.wf-table")
            if len(tables) >= 2:
                # First table is usually team 1
                rows1 = tables[0].select("tbody tr")
                for row in rows1:
                    player_cell = row.select_one("td.mod-player, td[class*='player']")
                    if player_cell:
                        name = player_cell.get("title") or player_cell.text.strip()
                        if name:
                            team1_players.add(normalize_name(name))
                
                # Second table is usually team 2
                rows2 = tables[1].select("tbody tr")
                for row in rows2:
                    player_cell = row.select_one("td.mod-player, td[class*='player']")
                    if player_cell:
                        name = player_cell.get("title") or player_cell.text.strip()
                        if name:
                            team2_players.add(normalize_name(name))
            
            # Alternative: single table with alternating rows
            else:
                rows = section.select("tbody tr")
                for i, row in enumerate(rows):
                    player_cell = row.select_one("td.mod-player, td[class*='player']")
                    if player_cell:
                        name = player_cell.get("title") or player_cell.text.strip()
                        if name:
                            normalized = normalize_name(name)
                            # First 5 rows usually team 1, next 5 team 2
                            if i < 5:
                                team1_players.add(normalized)
                            else:
                                team2_players.add(normalized)
        
        # Assign teams to player links
        for normalized_name, player_data in player_links.items():
            if normalized_name in team1_players:
                player_data['team'] = teams[0] if teams else None
            elif normalized_name in team2_players:
                player_data['team'] = teams[1] if len(teams) > 1 else None
    
    return {
        'players': player_links,
        'teams': teams,
        'match_url': match_url
    }

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
        return []

    normalized_player = normalize_name(player_name)
    
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
            break  # only one row per map for player

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
