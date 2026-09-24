# KGP Crew Monitoring System (CMS Portal)

A Flask-based web application for real-time monitoring and management of locomotive crew duties at KGP Lobby.

---

## ⚠️ CRITICAL DEPLOYMENT WARNING ⚠️

**ROTATE ALL SECRETS BEFORE DEPLOYMENT.**

This repository previously contained hardcoded credentials and service account keys in the source code and git history. **Removing them from the working tree does not remove them from git history.** 

Any credential that was ever hardcoded (including the discovered Google Cloud service-account JSON, CMS/RTIS portal credentials, Flask SECRET_KEY, API_SECRET, default admin passwords, cookies, and SSH keys) is considered **COMPROMISED** and must be permanently revoked or rotated in the respective upstream service console before this application is deployed to an environment accessible from the internet.

---

## Features

- **Crew Form** — Crew self-service duty report with GPS location capture, Relief toggle, BPC / Loco / Train number entry, and CTO time.
- **Crew List** — Live admin dashboard with search, CSV export, soft-delete (30-day retention), and ingest-miss highlighting.
- **CMS & RTIS Scrapers** — Background Selenium scrapers for CMS portal sync and RTIS live GPS locomotive tracking.
- **BPC Report** — Monthly report of BPC compliance by crew ID.
- **Admin Console** — Super-admin and multi-user admin access with role-based permissions and forced-password-change flow.
- **Soft Delete** — Records are flagged and highlighted in red; hard delete runs automatically after 30 days.

---

## Quick Start (Local Development)

### Prerequisites
- Python 3.11+
- Google Chrome + matching ChromeDriver (in `chromedriver-win64/` on Windows or system on Linux)
- Windows: run directly; Linux/Mac: use `start_prod.sh`

### Steps

```bash
# 1. Clone
git clone <repo-url>
cd "cms automate"

# 2. Create virtual environment
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # Linux/Mac

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure Secrets (REQUIRED)
cp .env.example .env
# Edit .env to set SECRET_KEY, API_SECRET, BOOTSTRAP_ADMIN_* and portal credentials.
# The app WILL NOT START if these are missing.

# 5. Start (Windows one-click)
start_dev.bat

# OR manually:
python webapp/app.py
```

Open http://localhost:5000

---

## Production Deployment

### Environment Variables

Copy `.env.example` to `.env` and fill in real values. See `.env.example` for the full list of required variables, including:

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | ✅ | Random 32-byte hex for Flask sessions |
| `API_SECRET` | ✅ | Shared secret between webapp and scraper |
| `BOOTSTRAP_ADMIN_USERNAME` | ✅ | First-run admin username (ignored after creation) |
| `BOOTSTRAP_ADMIN_PASSWORD` | ✅ | First-run admin password |
| `GCP_CREDENTIALS_B64` | — | Base64-encoded Google Vision API service account JSON |

> **Note on CMS/RTIS credentials:** These are **not** set via environment variables. After deployment, log in as admin and go to **Admin → Settings** to enter CMS username/password and RTIS username/password. Indian Railways forces a 6-month credential update cycle and requires a daily OTP — credentials are stored in the database so admins can update them without redeployment.

### Option A — Bare Metal / VM (Linux)

```bash
chmod +x start_prod.sh
./start_prod.sh
```

### Option B — Docker

```bash
# Build
docker build -t cms-portal .

# Run (Mount .env file and data volume)
docker run -d \
  --name cms-portal \
  -p 8080:8080 \
  -v cms_data:/app/data \
  --env-file .env \
  cms-portal
```

*(If using Coolify, inject the required secrets into the Environment Variables panel).*

### Reverse Proxy (Nginx example)

```nginx
server {
    listen 80;
    server_name cms.yourdomain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name cms.yourdomain.com;

    ssl_certificate     /etc/ssl/certs/cms.crt;
    ssl_certificate_key /etc/ssl/private/cms.key;

    location / {
        proxy_pass         http://127.0.0.1:8080;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

---

## Project Structure

```
cms automate/
├── webapp/
│   ├── app.py              # Flask application factory
│   ├── routes/             # App Blueprints (crew, admin, api, etc.)
│   ├── wsgi.py             # WSGI entry point for gunicorn
│   ├── templates/          # Jinja2 HTML templates
│   └── static/             # CSS / JS assets
├── scraper/
│   ├── login.py            # Selenium CMS scraper
│   └── rtis_scraper.py     # RTIS Live Tracker scraper
├── data/                   # SQLite database (gitignored)
├── Dockerfile
├── gunicorn.conf.py        # Gunicorn production settings
├── requirements.txt
├── start_dev.bat           # Windows dev launcher
├── start_prod.sh           # Linux/Mac production launcher
├── .env.example            # Environment variable template
└── README.md
```

---

## Database

SQLite at `data/crew.db`. 

Records are soft-deleted (remain visible, highlighted red). Hard delete runs automatically after **30 days** via a background thread.

---

## Security Notes

- No secrets are stored in the database.
- Provide all configuration via `.env` or system environment variables.
- Serve behind HTTPS — `SESSION_COOKIE_SECURE=True` is auto-enabled when `FLASK_ENV=production`.
- The `/api/sync` and internal endpoints are protected by `API_SECRET` using constant-time comparison.
