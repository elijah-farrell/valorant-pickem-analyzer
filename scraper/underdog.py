import requests

def get_pickem_slate(sport_id="VAL"):
    url = f"https://api.underdogfantasy.com/v2/pickem_search/search_results?sport_id={sport_id}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        return response.json().get("over_under_lines", [])
    except requests.RequestException:
        return []

