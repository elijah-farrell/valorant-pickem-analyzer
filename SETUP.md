# Setup Guide

Instructions for running the Valorant Pick'em Analyzer locally.

---

## Prerequisites

- Python 3.7 or higher
- pip (Python package manager)

---

## Installation

1. **Install dependencies**

   ```powershell
   pip install -r requirements.txt
   ```

2. **Run the Flask server**

   ```powershell
   python app.py
   ```

3. **Open your browser**

   Navigate to `http://localhost:5000`

---

## Project Structure

```
valorant-pickem-analyzer/
├── app.py                 # Flask backend server
├── README.md
├── SETUP.md              # This file
├── requirements.txt  # Python dependencies
├── static/
│   ├── index.html        # Frontend HTML
│   ├── styles.css        # Modern CSS styling
│   └── app.js            # Frontend JavaScript
├── scraper/
│   ├── vlr.py           # VLR.gg scraping functions
│   └── underdog.py       # Underdog Fantasy API client
```

---

## API Endpoints

- `GET /api/slate` - Get Underdog slate with VLR stats comparison
- `GET /api/player/<player_name>` - Get detailed stats for a specific player

---

## Libraries Used

- **[Flask](https://flask.palletsprojects.com/):** Web framework for the backend API
- **[flask-cors](https://flask-cors.readthedocs.io/):** Cross-Origin Resource Sharing (CORS) support
- **[flask-limiter](https://flask-limiter.readthedocs.io/):** Rate limiting for API endpoints
- **[requests](https://docs.python-requests.org/):** Fetching web pages and APIs
- **[beautifulsoup4](https://www.crummy.com/software/BeautifulSoup/):** HTML parsing for scraping VLR.gg
- **Python stdlib:** datetime, os, traceback, urllib.parse, re

---

## Troubleshooting

**Port 5000 already in use?**  
Change the port in `app.py`: `app.run(debug=True, port=5001)`

**Scraping not working?**  
VLR.gg may have changed their page structure. Check the Flask console for debug messages.

**Player not found?**  
Make sure the player name matches how it appears on VLR.gg (case-insensitive, but spelling must match).

---