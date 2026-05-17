# Setup Guide

Instructions for running the Valorant Pick'em Analyzer locally and deploying it.

---

## Architecture

| Piece | Role |
|-------|------|
| **`static/`** | Frontend (HTML, JS, CSS) — Vercel in production |
| **`app.py`** | Flask API — Gunicorn on Render in production |
| **`clients/`** | Underdog Fantasy API |
| **`scrapers/`** | VLR.gg HTML scraping |
| **`.venv/`** | Local virtual environment (not committed) |

**Local:** One process (`python app.py`) serves the UI and API at `http://127.0.0.1:5000` (or `localhost`).

**Production:** Vercel hosts the frontend; Render hosts the API. The browser calls Render from your Vercel URL (CORS).

The backend treats the environment as **development** when `ALLOWED_ORIGINS` is not set (local). In production, set `ALLOWED_ORIGINS` on Render.

---

## Prerequisites

- **Python 3.12+** from [python.org/downloads](https://www.python.org/downloads/) (use the **standalone Windows installer**, e.g. 3.14.x — not the Microsoft Store stub)
- During install: check **“Add python.exe to PATH”** — use **Install Now** (per-user); admin / “all users” is not required

---

## Windows: Install Python

1. Download the **standalone installer** from the main downloads page (not the 3.12.x “source only” security release pages).
2. Run the installer, check **Add python.exe to PATH**, click **Install Now**.
3. **Close and open a new terminal** (Cursor: Terminal → New Terminal).
4. Verify (use `where.exe` in PowerShell — `where` alone is a different command):

   ```powershell
   where.exe python
   python --version
   ```

   You should see something like `...\Programs\Python\Python314\python.exe` and `Python 3.14.x`.

**If `python` is not found** but you just installed, refresh PATH in the current session:

```powershell
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
```

**Optional:** Settings → Apps → Advanced app settings → **App execution aliases** → turn **Off** `python.exe` and `python3.exe` if they point at the Microsoft Store instead of your install.

**Do not rely on** `C:\Users\...\WindowsApps\python.exe` — that is often a Store redirect, not a full Python install.

---

## Local development

### 1. Create a virtual environment (recommended)

From the project root:

```powershell
cd C:\Users\Elijah\Documents\Code\websites\valorant-pickem-analyzer

python -m venv .venv

.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

In Cursor: **Ctrl+Shift+P** → **Python: Select Interpreter** → choose `.venv\Scripts\python.exe`.

### 2. Run the app

```powershell
python app.py
```

Open **http://127.0.0.1:5000** or **http://localhost:5000** (both work; the frontend uses the same origin for API calls).

### 3. What to expect

- **Load Underdog Slate** can take **10–20+ minutes** (many players × VLR match pages). Progress updates stream over SSE; let it finish.
- **Search player** is faster (single player).

---

## Project structure

```
valorant-pickem-analyzer/
├── app.py                 # Flask app (routes, background jobs, SSE progress)
├── requirements.txt       # Python dependencies (includes gunicorn)
├── vercel.json            # Vercel static hosting
├── SETUP.md
├── README.md
├── static/                # Frontend (Vercel)
│   ├── index.html
│   ├── app.js
│   └── styles.css
├── clients/
│   └── underdog.py
├── scrapers/
│   └── vlr.py
└── .venv/                 # Local only — create with python -m venv .venv
```

---

## API endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Frontend (local) or API info (production) |
| `GET /health` | Health check |
| `GET /api/slate` | Start slate job; returns `{ "job_id": "..." }` |
| `GET /api/progress/<job_id>` | SSE progress (local dev) |
| `GET /api/progress/<job_id>/status` | Poll progress (production / Render) |
| `GET /api/player/<name>` | Single-player VLR stats |

---

## Deployment

### Backend (Render)

Configure everything in the [Render dashboard](https://dashboard.render.com)
1. **New → Web Service** → connect your GitHub repo.
2. **Settings** (typical values):
   - **Runtime:** Python 3
   - **Build command:** `pip install -r requirements.txt`
   - **Start command** (required — do **not** use `python app.py`; no leading/trailing spaces):
     ```bash
     gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 0 app:app
     ```
     `--workers 1` keeps in-memory job progress on one process. `--timeout 0` allows long requests if needed.
3. **Environment** tab → add:
   | Key | Value |
   |-----|--------|
   | `FLASK_ENV` | `production` |
   | `ALLOWED_ORIGINS` | Your Vercel URL(s), comma-separated, e.g. `https://your-app.vercel.app,https://your-app-git-main.vercel.app` |
   | `MAX_MATCHES` | `15` (optional — fewer VLR pages per player so slate finishes before Render restarts; default is `40`) |
4. **Save** and deploy.

After deploy, logs should show **gunicorn** listening — not `Debug mode: on` or `Restarting with stat` (those mean the start command is still `python app.py`).

**Free tier:** Service may spin down or restart during long jobs (~15+ min). The Vercel site uses **HTTP polling** for progress on Render (not long-lived SSE). For reliable full slates, run locally. On Render, set `MAX_MATCHES=15` to finish faster.

### Frontend (Vercel)

1. Connect the repo; output directory is `static` (`vercel.json`).
2. `static/index.html` sets the API base URL:
   - **Production (Vercel):** `https://valorant-pickem-analyzer.onrender.com/api`
   - **Local (`localhost` or `127.0.0.1`):** same origin `/api` (Flask serves both)

---

## Libraries

- **Flask** — API and local static hosting
- **gunicorn** — production WSGI server on Render
- **flask-cors** — CORS for Vercel → Render
- **flask-limiter** — rate limits on API routes
- **requests** — HTTP for Underdog + VLR
- **beautifulsoup4** — VLR HTML parsing

---

## Troubleshooting

**`python` not found after install**  
Open a **new** terminal, or refresh PATH (see Windows section above). Use `where.exe python`, not `where python`.

**CORS error calling Render from `127.0.0.1`**  
Use local dev only against your local Flask server (`python app.py`). Production API is for the Vercel site; local uses `window.location.origin + '/api'`.

**Slate stuck loading / connection lost**  
Large slates take a long time. If the progress stream drops (common on free Render), retry or run locally. Check the red error box on the page for the last step seen.

**Port 5000 in use**  
```powershell
$env:PORT=5001; python app.py
```
Then open `http://127.0.0.1:5001`.

**Player not found**  
Name must match VLR.gg spelling (case-insensitive).

**Scraping errors**  
VLR.gg markup may have changed; check the terminal running `app.py`.

---

## Quick reference

| Task | Command |
|------|---------|
| Create venv | `python -m venv .venv` |
| Activate (PowerShell) | `.\.venv\Scripts\Activate.ps1` |
| Install deps | `pip install -r requirements.txt` |
| Run locally | `python app.py` |
| Production server | `gunicorn ... app:app` (see Render section) |
