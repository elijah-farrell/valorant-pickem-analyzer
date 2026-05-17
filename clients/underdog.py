import requests

BASE_URL = "https://api.underdogfantasy.com/v1/over_under_lines?sport_id=val"

def get_pickem_slate():
    url = BASE_URL
    try:
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return {}
