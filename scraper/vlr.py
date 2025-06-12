import requests
from datetime import datetime
from bs4 import BeautifulSoup
import pandas as pd 
import os

os.makedirs("data", exist_ok=True)

BASE_URL = "https://www.vlr.gg"

def normalize_name(name: str) -> str:
    return name.lower().replace(" ", "").replace(".", "").replace("-", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
}

def fetch_soup(url: str):
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        return BeautifulSoup(res.text, "html.parser")
    except requests.RequestException as e:
        print(f"Error fetching {url}: {e}")
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
            return BASE_URL + tag['href']

    if player_links:
        return BASE_URL + player_links[0]['href']

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
    match_history_url = player_url.replace("/player/", "/player/matches/")
    soup = fetch_soup(match_history_url)
    if not soup:
        return []

    match_links = []
    all_links = soup.select("a[href^='/']")
    for a in all_links:
        href = a.get('href')
        if href:
            parts = href.split('/')
            if len(parts) > 1 and parts[1].isdigit():
                full_link = BASE_URL + href
                if full_link not in match_links:
                    match_links.append(full_link)
    return match_links

def get_match_title(soup):
    team_tags = soup.select("div.match-header-link-name .wf-title-med")
    if len(team_tags) >= 2:
        team1 = team_tags[0].text.strip()
        team2 = team_tags[1].text.strip()
        return f"{team1} vs {team2}"
    return "<Unknown Match>"

def get_match_date(soup):
    date_tag = soup.select_one(".match-header-date .moment-tz-convert")
    if date_tag:
        try:
            raw = date_tag.get("data-utc-ts")
            if raw and ":" in raw:
                dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
                return dt.strftime("%Y-%m-%d")
        except Exception as e:
            print(f"  [!] Date parse error: {e}")
    return "<Unknown Date>"

def parse_match_page(match_url, player_name):
    soup = fetch_soup(match_url)
    if not soup:
        return []

    maps_data = []
    match_title = get_match_title(soup)
    match_date = get_match_date(soup)

    map_sections = soup.select("div.vm-stats-game")
    if not map_sections:
        print(f"  [!] No map sections found on {match_url}")
        return []

    for section in map_sections:
        map_label = section.select_one(".vm-stats-game-header .map span")
        if not map_label:
            continue

        map_name = " ".join(map_label.text.replace("PICK", "").split()).strip()
        if not map_name or map_name.lower() in ("all maps", "<unknown map>"):
            continue

        rows = section.select("table.wf-table-inset tbody tr")
        if not rows:
            continue

        for row in rows:
            player_cell = row.select_one("td.mod-player div.text-of")
            if not player_cell:
                continue
            name = player_cell.get("title") or player_cell.text.strip()
            if normalize_name(name) != normalize_name(player_name):
                continue

            agent_img = row.select_one("td img")
            agent = agent_img["alt"].strip() if agent_img else "<Unknown Agent>"

            kills_cell = row.select_one("td.mod-stat.mod-vlr-kills")
            kills = 0
            if kills_cell:
                both = kills_cell.select_one(".mod-both")
                if both:
                    try:
                        kills = int(both.text.strip())
                    except:
                        kills = 0

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
