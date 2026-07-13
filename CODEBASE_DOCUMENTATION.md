# CMS Automate Codebase Documentation

This document serves as a comprehensive guide to the `cms-automate` codebase. It details the architecture, file structures, database schemas, and the line-by-line working of critical components to ensure any language model or developer can instantly understand the project.

---

## 1. High-Level Architecture
The project is a hybrid system consisting of:
1. **Flask Web Application (`webapp/`)**: Serves the user interface (Crew List Monitor, Admin Dashboards, Crew Login), provides internal APIs, manages a SQLite database, and orchestrates background jobs.
2. **Selenium Scraper (`scraper/`)**: Runs in the background (managed by the Flask app), automatically logs into the Indian Railways CMS portal, solves CAPTCHAs using Google Cloud Vision OCR, extracts crew sign-on and TA data, and pushes this data via REST APIs back to the Flask Web Application.

---

## 2. Directory & File Breakdown

### Root Directory
- **`Dockerfile` & `.dockerignore`**: Defines the containerized environment. Installs necessary dependencies including Python, Google Chrome, and Chromedriver for headless scraping.
- **`entrypoint.sh` & `start_prod.sh` / `start_dev.bat`**: Startup scripts that manage virtual environments, run migrations, and start the Gunicorn server (`wsgi.py`).
- **`requirements.txt`**: Lists all Python dependencies (Flask, Selenium, BeautifulSoup4, Pandas, google-cloud-vision, etc.).

### Web Application (`webapp/`)

#### `webapp/app.py`
This is the core of the web server. It manages the following responsibilities:
- **Configuration & Setup**: Sets up Flask, session lifetimes, secure cookies, and environment variables.
- **Background Threads**:
  - `run_scraper_thread()`: Spawns the scraper as a `subprocess.Popen`. It monitors `data/stop.txt` to implement a graceful, soft-stop mechanism when the admin triggers a cancellation. It restarts the scraper automatically if it crashes or completes its sleep cycle.
  - `run_cleanup_thread()`: Runs continuously to delete old crew submissions and records older than 30 days from the SQLite database.
- **Database Initializer (`init_db`)**: Creates necessary SQLite tables (`crew_records`, `crew_submissions`, `booking_ta_crew`, `admin_edits`, `cms_settings`, `users`, `signup_passcodes`).
- **Decorators (`@login_required`, `@super_admin_required`)**: Protects admin routes using session cookies.
- **User Routes**: 
  - `/crew_login` & `/form`: Endpoints for crew members to manually log their details, current location, relief data, and CTO time.
  - `/submit`: Receives form submissions from the crew and records them in `crew_submissions`. Automatically handles relief crew handover logic (inheriting locomotive details from the previous crew).
- **Admin Routes**:
  - `/admin`: Dashboard showing the live status of the background scraper. Allows manual trigger and cancellation.
  - `/admin/settings`: Manages the CMS portal login credentials (saved in the `cms_settings` table).
  - `/admin/reports`: Displays submission statistics, highlighting crews that have or have not entered BPC details in the last 30 days.
  - `/crew_list`: The primary monitor page. It executes a complex SQL `UNION` and `COALESCE` query to merge live scraped data (`crew_records`) with manual crew submissions (`crew_submissions`) and admin overrides (`admin_edits`). Priority logic: Admin Edit > Crew Submission > Scraped CMS Data.
- **Internal APIs (Webhook for Scraper)**:
  - `/api/scraper/state`: The scraper constantly POSTs its state (Running, Sleeping, Error, Captcha Waiting) here.
  - `/api/sync` & `/api/sync_ta`: Endpoints where the scraper pushes scraped JSON data. The app parses this data and updates/inserts records into the `crew_records` and `booking_ta_crew` tables. Missing records (crews that signed off) are flagged by tracking `ingest_miss_count`.

#### `webapp/templates/`
Contains Jinja2 HTML templates.
- **`crew_list.html`**: A highly interactive UI using Vanilla JS and CSS. Displays the combined crew data. It supports inline editing (`contenteditable`), autocomplete for stations, and live duty-hour gradient color calculations based on sign-on time.
- **`admin.html`**: Shows scraper state and provides the soft-stop/start buttons. Displays base64 CAPTCHA images if human intervention is required.
- **`form.html`**: The form used by crews to submit their location/relief/CTO.

### Scraper (`scraper/`)

#### `scraper/login.py`
This is a robust, fault-tolerant Selenium scraper.
- **Human Simulation**: Uses `human_delay` and `human_type` functions to mimic real user interaction (random delays between keystrokes) to bypass bot detection.
- **Main Loop (`main_loop`)**: An infinite `while True` loop that runs the scraping lifecycle.
  - **Soft-Stop Check**: Constantly checks `check_stop_signal()` during `interruptible_sleep`. If triggered, it exits gracefully by quitting the driver and killing the Python process.
  - **Cookie Reuse**: Attempts to use saved cookies from `data/cookies.json` to bypass the login flow. If cookies are valid, it proceeds directly to `sync_signon_reports` and `sync_ta_reports`.
  - **Login Flow**: If cookies are invalid, spawns a headless Chrome browser.
  - **CAPTCHA Solving**: Extracts the base64 CAPTCHA image, sends it to Google Cloud Vision API (`vision.ImageAnnotatorClient().text_detection`), and types the resulting text.
  - **Error Handling**: Detects `"User Id/Password Does Not Match"` and counts failures. After 2 failures, writes to `data/fatal_error.txt` and exits, preventing account lockouts. Detects `"Please Enter Valid Captcha!"` and restarts the login flow.
- **Data Syncing**:
  - `sync_signon_reports`: Navigates the CMS report portal using `requests.Session()` with complex XML payloads. It scrapes the drill-down tables for active crews and POSTs to `/api/sync`.
  - `sync_ta_reports`: Similar logic but fetches "Booking On TA" records, parsing tables with BeautifulSoup and POSTing to `/api/sync_ta`.

#### `scraper/cms_autom.py`
A utility script for parsing and formatting daily exported CSV reports.
- Reads `CMS_REPORT.csv` using `pandas`.
- Splits the data into separate DataFrames based on unique `ABN.TYPE` values.
- Uses `openpyxl` to generate beautifully formatted `.xlsx` files with specific fonts (Aptos Display), rotated header text, auto-adjusted column widths, and cell borders.
- Saves the files into a `Date_wise/<DD-MM-YYYY>/` directory structure.

---

## 3. Database Schema (`crew.db`)
Located in `data/crew.db`. Uses SQLite.
- **`crew_records`**: Stores live data scraped directly from the CMS portal (`from_sttn`, `sign_on_time`, `duty_hrs`, etc.).
- **`crew_submissions`**: Stores manual form entries submitted by crew members. Includes locations, CTO times, and relief data.
- **`admin_edits`**: A key-value table tracking manual overrides applied by the admin directly from the `crew_list` table UI.
- **`booking_ta_crew`**: Stores scraped TA (Traveling Allowance) data such as mobile numbers and ordering times.
- **`users` & `signup_passcodes`**: Authentication tables for the Admin portal.
- **`cms_settings`**: Stores the encrypted/plaintext CMS login credentials used by `login.py`.

---

## 4. Key Execution Flows

### The Scraping Cycle
1. `app.py` thread executes `subprocess.Popen(['python', 'scraper/login.py'])`.
2. `login.py` attempts to fetch data using saved cookies via headless requests.
3. If the server responds with a login page (expired session), `login.py` launches a headless Chrome window, pulls credentials from the DB, solves the OCR CAPTCHA, and logs in.
4. `login.py` performs POST requests to the CMS portal, scrapes the resulting HTML tables with `BeautifulSoup`, and pushes the JSON objects back to `http://localhost:5000/api/sync`.
5. `app.py` receives the JSON, updates `crew_records`, and tracks missing crews.
6. `login.py` saves the new session cookies to `data/cookies.json`, goes to sleep for 25-35 minutes, and repeats.

### The Crew List Render Cycle
1. Admin visits `/crew_list`.
2. `app.py` executes a `UNION` SQL query joining `crew_records` and `crew_submissions`.
3. In Python, the code iterates over the rows and applies priority logic: if an `admin_edit` exists for a field, it takes precedence. Otherwise, it checks `crew_submissions`, and finally falls back to `crew_records` (CMS Data).
4. Jinja loops through the final data array, injecting it into `crew_list.html`.
5. Vanilla JS in the frontend calculates live duty durations and colors the cells in a gradient based on how long the crew has been on duty.
6. **PDD Calculation**: Handled dynamically in `app.py`. For NMP and NPTY (which are treated as equivalent), if sign-on and CTO match (or are in the NMP/NPTY/KGP combination), the PDD is calculated.
7. **Location & Relief UI**: The `crew_list.html` displays the current location along with the extracted time part. Checking the "Relief" toggle auto-populates the current local time via frontend JS.

## 5. Usage for LLM / Agents
When tasked with modifying this codebase:
- **UI Changes**: Look at `webapp/templates/` and corresponding routes in `webapp/app.py`. Note that `crew_list.html` uses heavy DOM manipulation.
- **Scraping Changes**: Edit `scraper/login.py`. Be mindful of `interruptible_sleep` and the `check_stop_signal()` logic when adding new waits or API calls.
- **Database Changes**: Always update the `init_db()` function in `app.py` with `ALTER TABLE` fallbacks to handle schema migrations gracefully.
