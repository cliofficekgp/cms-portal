# KGP Crew Monitoring System (CMS Portal)

A Flask-based web application for real-time monitoring and management of locomotive crew duties at KGP Lobby.

---

## Features

- **Crew Form** — Crew self-service duty report with GPS location capture, Relief toggle, BPC / Loco / Train number entry, and CTO time.
- **Crew List** — Live admin dashboard with search, CSV export, soft-delete (30-day retention), and ingest-miss highlighting.
- **CMS Scraper** — Background Selenium scraper that syncs crew sign-on data from the Indian Railways CMS portal.
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

# 4. Start (Windows one-click)
start_dev.bat

# OR manually:
python webapp/app.py
```

Open http://localhost:5000

**Default admin login:** `admin` / `Cms@123` — **change immediately in the Admin Settings page.**

---

## Production Deployment

### Environment Variables

Copy `.env.example` to `.env` and fill in real values:

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | ✅ | Random 32-byte hex — run `python -c "import secrets; print(secrets.token_hex(32))"` |
| `API_SECRET` | ✅ | Shared secret between webapp and scraper |
| `FLASK_ENV` | ✅ | Set to `production` |
| `PORT` | — | Default `8080` |
| `DB_PATH` | — | Absolute path to SQLite file. Default: `<project>/data/crew.db` |

### Option A — Bare Metal / VM (Linux)

```bash
# Set env vars, then:
chmod +x start_prod.sh
./start_prod.sh
```

### Option B — Docker

```bash
# Build
docker build -t cms-portal .

# Run (mount a volume for persistent DB)
docker run -d \
  --name cms-portal \
  -p 8080:8080 \
  -v cms_data:/app/data \
  -e SECRET_KEY=your_secret_here \
  -e API_SECRET=your_api_secret_here \
  -e FLASK_ENV=production \
  cms-portal
```

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
│   ├── app.py              # Flask application + all routes
│   ├── wsgi.py             # WSGI entry point for gunicorn
│   ├── templates/          # Jinja2 HTML templates
│   └── static/             # CSS / JS assets
├── scraper/
│   └── login.py            # Selenium CMS scraper
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

SQLite at `data/crew.db`. Two core tables:

- **`crew_records`** — CMS-synced crew sign-on records (one row per crew per duty)
- **`crew_submissions`** — Manual duty-form submissions from crew

Records are soft-deleted (remain visible, highlighted red). Hard delete runs automatically after **30 days** via a background thread.

---

## Security Notes

- Change the default `admin` / `Cms@123` credentials **immediately** after first login.
- Set a strong `SECRET_KEY` (32+ random bytes) in production.
- Serve behind HTTPS — `SESSION_COOKIE_SECURE=True` is auto-enabled when `FLASK_ENV=production`.
- The `/api/sync` and `/api/scraper/*` endpoints are protected by `API_SECRET`.
