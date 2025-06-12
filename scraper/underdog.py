import requests

def get_pickem_slate(sport_id="VAL"):
    url = f"https://api.underdogfantasy.com/v2/pickem_search/search_results?sport_id={sport_id}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        return response.json().get("over_under_lines", [])
    except requests.RequestException:
        return []

def print_pickem_summary(item):
    ou = item.get("over_under", {})
    player_name = ou.get("title", "Unknown Player")
    line_value = item.get("stat_value", "N/A")

    options = item.get("options", [])
    odds_over = options[0].get("american_price", "N/A") if len(options) >= 1 else "N/A"
    odds_under = options[1].get("american_price", "N/A") if len(options) >= 2 else "N/A"

    print(f"🔹 {player_name:<20} | Line: {line_value:>5}")
    print(f"   📊 Odds: Over {odds_over:>5}  |  Under {odds_under:>5}\n")
