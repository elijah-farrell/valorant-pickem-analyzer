# Setup Guide

Instructions for running the Valorant Pick'em Analyzer locally.

---

## Architecture

This project uses a **separated frontend/backend architecture**:
- **Frontend**: Static files in `static/` folder, deployed to Vercel
- **Backend**: Flask API in `app.py`, deployed to Render
- **Local Development**: Both can run locally for testing

---

## Prerequisites

- Python 3.7 or higher
- pip (Python package manager)

---

## Local Development

**Easy setup - just one command!**

1. **Install dependencies**

   ```powershell
   pip install -r requirements.txt
   ```

2. **Run the server**

   ```powershell
   python app.py
   ```

3. **Open your browser**

   Navigate to `http://localhost:5000`

That's it! The Flask server automatically serves the frontend in development mode.

**How it works:**
- **Development** (local): Flask serves both API and frontend files
- **Production** (Render): Flask only serves API endpoints (frontend is on Vercel)

The app detects development mode automatically (no `ALLOWED_ORIGINS` env var = dev mode).

---

## Project Structure

```
valorant-pickem-analyzer/
├── app.py                 # Flask backend API (Render)
├── vercel.json            # Vercel config for frontend
├── README.md
├── SETUP.md              # This file
├── requirements.txt       # Python dependencies (backend)
├── static/               # Frontend files (Vercel)
│   ├── index.html        # Frontend HTML
│   ├── styles.css        # CSS styling
│   ├── app.js            # Frontend JavaScript
│   └── favicon.ico       # Site favicon
├── scraper/              # Backend scrapers
│   ├── vlr.py           # VLR.gg scraping functions
│   └── underdog.py       # Underdog Fantasy API client
```

---

## API Endpoints

- `GET /` - API info and available endpoints
- `GET /health` - Health check endpoint
- `GET /api/slate` - Get Underdog slate with VLR stats comparison
- `GET /api/player/<player_name>` - Get detailed stats for a specific player

---

## Deployment

### Backend (Render)

1. Connect your GitHub repo to Render
2. Set environment variable:
   - `ALLOWED_ORIGINS`: Comma-separated list of Vercel domains
     - Example: `https://your-app.vercel.app,https://your-app-git-main.vercel.app`
     - Include both production and preview URLs
3. The backend will automatically handle CORS for these domains

### Frontend (Vercel)

1. Connect your GitHub repo to Vercel
2. Set output directory to `static` (configured in `vercel.json`)
3. The frontend automatically detects the environment and uses the correct API URL:
   - Production: `https://valorant-pickem-analyzer.onrender.com/api`
   - Local dev: `http://localhost:5000/api`

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