import os, time, datetime, base64, io, json, traceback, zoneinfo, socket, random, math
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from PIL import Image

# -----------------------------------------------------------------------------
# Timing & State Helpers
# -----------------------------------------------------------------------------
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, 'data')

def check_stop_signal():
    stop_file = os.path.join(DATA_DIR, 'rtis_stop.txt')
    return os.path.exists(stop_file)

def check_run_now_signal():
    run_now_file = os.path.join(DATA_DIR, 'rtis_run_now.txt')
    if os.path.exists(run_now_file):
        try: os.remove(run_now_file)
        except OSError: pass
        return True
    return False

def interruptible_sleep(sleep_seconds):
    for _ in range(int(sleep_seconds)):
        if check_stop_signal(): return True
        if check_run_now_signal(): return False
        time.sleep(1)
    if sleep_seconds > int(sleep_seconds):
        time.sleep(sleep_seconds - int(sleep_seconds))
    return check_stop_signal()

def human_delay(min_s, max_s):
    return interruptible_sleep(random.uniform(min_s, max_s))

def human_type(element, text, min_delay=0.06, max_delay=0.18):
    for ch in text:
        if check_stop_signal(): return True
        element.send_keys(ch)
        time.sleep(random.uniform(min_delay, max_delay))
    return False

def random_cycle_sleep_seconds(min_minutes=12, max_minutes=18):
    return random.uniform(min_minutes * 60, max_minutes * 60)

def haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371000 # radius of earth in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
FLASK_API_URL = f"http://127.0.0.1:{os.environ.get('PORT', 5000)}/api/rtis"
API_SECRET = os.environ.get('API_SECRET', 'cms-sync-secret-key-2026')
IST = zoneinfo.ZoneInfo("Asia/Kolkata")
ACTIVE_TUNNEL = "Unknown"

# -----------------------------------------------------------------------------
# Network / Proxy
# -----------------------------------------------------------------------------
def is_proxy_available(host, port, retries=2, delay=2):
    port_open = False
    for attempt in range(retries):
        try:
            with socket.create_connection((host, port), timeout=2):
                port_open = True
                break
        except OSError:
            if attempt < retries - 1:
                time.sleep(delay)
    if not port_open: return False
    
    try:
        test_session = requests.Session()
        test_session.proxies = {'http': f'socks5h://{host}:{port}', 'https': f'socks5h://{host}:{port}'}
        resp = test_session.get('https://rtis.indianrail.gov.in/RTISDashboardUI/', timeout=10)
        return True
    except Exception:
        return False

def get_active_proxy():
    global ACTIVE_TUNNEL
    host = os.environ.get("SOCKS_PROXY_HOST", "127.0.0.1")
    
    print("[RTIS-Proxy] Testing Primary Tunnel on port 1080...", flush=True)
    if is_proxy_available(host, 1080):
        ACTIVE_TUNNEL = "Office PC (1080)"
        return host, 1080
        
    print("[RTIS-Proxy] Testing Fallback Tunnel on port 1081...", flush=True)
    if is_proxy_available(host, 1081):
        ACTIVE_TUNNEL = "Laptop (1081)"
        return host, 1081
        
    if os.environ.get('ENV', 'local').lower() == 'production':
        print("[RTIS-Proxy] WARNING: Both tunnels down. Production environment detected. Blocking direct connection.", flush=True)
        ACTIVE_TUNNEL = "Offline"
        return None, None
        
    print("[RTIS-Proxy] WARNING: Both tunnels down. Local environment detected. Attempting direct connection...", flush=True)
    ACTIVE_TUNNEL = "Direct (No Proxy)"
    return None, None

def send_state_to_admin(status, message, action_required=False, action_type='', last_ddddocr_failure=None, image_base64='', phone_number=None):
    payload = {
        'status': status,
        'message': message,
        'action_required': action_required,
        'action_type': action_type,
        'active_tunnel': ACTIVE_TUNNEL,
        'image_base64': image_base64
    }
    if phone_number:
        payload['phone_number'] = phone_number
    if last_ddddocr_failure: payload['last_ddddocr_failure'] = last_ddddocr_failure
    try: requests.post(f"{FLASK_API_URL}/state", json=payload, headers={'X-API-Secret': API_SECRET}, timeout=5)
    except: pass

def wait_for_admin_input(timeout_seconds=300):
    start = time.time()
    while time.time() - start < timeout_seconds:
        if check_stop_signal(): return None
        try:
            resp = requests.get(f"{FLASK_API_URL}/action", headers={'X-API-Secret': API_SECRET}, timeout=5)
            data = resp.json()
            if data.get('submitted_value'): return data['submitted_value']
        except: pass
        time.sleep(2)
    return None

# -----------------------------------------------------------------------------
# Main Loop
# -----------------------------------------------------------------------------
def main_loop():
    print("[RTIS] Starting scraper...", flush=True)
    
    # We load ddddocr inside the subprocess to avoid multiprocessing fork issues
    import ddddocr
    import logging
    logging.getLogger('ddddocr').setLevel(logging.WARNING)
    ocr = ddddocr.DdddOcr(show_ad=False)
    
    consecutive_captcha_failures = 0
    consecutive_auth_failures = 0
    
    while True:
        if check_stop_signal():
            sys.exit(0)
            
        proxy_host, proxy_port = get_active_proxy()
        
        if not proxy_host and ACTIVE_TUNNEL == "Offline":
            send_state_to_admin('error', 'Error: Cannot connect to RTIS Server. Production environment blocks direct connections.')
            interruptible_sleep(60)
            continue
            
        try:
            # 1. Fetch Credentials
            import sqlite3
            conn = sqlite3.connect(os.path.join(DATA_DIR, 'crew.db'), timeout=10.0)
            cur = conn.cursor()
            cur.execute('SELECT rtis_username, rtis_password FROM cms_settings WHERE id = 1')
            row = cur.fetchone()
            conn.close()
            
            rtis_user, rtis_pass = row if row else ('', '')
            if not rtis_user or not rtis_pass:
                send_state_to_admin('error', 'No RTIS credentials configured. Please set them in Admin Settings.')
                if interruptible_sleep(60): sys.exit(0)
                continue
                
            send_state_to_admin('starting', f'Launching Chrome using tunnel {ACTIVE_TUNNEL}...')
            options = Options()
            if proxy_host:
                options.add_argument(f'--proxy-server=socks5://{proxy_host}:{proxy_port}')
            options.add_argument('--headless=new')
            options.add_argument('--disable-gpu')
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            options.add_argument('--window-size=1280,800')

            # Detect Chromium binary path (Linux vs Windows)
            chrome_bin = os.environ.get('CHROME_BIN', '')
            if not chrome_bin:
                for candidate in [
                    '/usr/bin/chromium-browser',   # Ubuntu (Oracle Cloud)
                    '/usr/bin/chromium',            # Debian/other Linux
                    '/usr/bin/google-chrome',       # Google Chrome on Linux
                ]:
                    if os.path.exists(candidate):
                        chrome_bin = candidate
                        break
            if chrome_bin:
                options.binary_location = chrome_bin

            # Detect chromedriver path
            chromedriver_path = os.environ.get('CHROMEDRIVER_PATH', '')
            if chromedriver_path:
                from selenium.webdriver.chrome.service import Service
                driver = webdriver.Chrome(service=Service(chromedriver_path), options=options)
            else:
                driver = webdriver.Chrome(options=options)
            driver.set_page_load_timeout(45)

            # 2. Login Page & Captcha
            send_state_to_admin('running', 'Loading RTIS Login Page...')
            driver.get("https://rtis.indianrail.gov.in/RTISDashboardUI/login")
            if human_delay(2, 4): sys.exit(0)

            # Check if OTP page is already active
            if "OTP" in driver.page_source or "verifyOTP" in driver.page_source:
                pass # Go to OTP block
            elif "shedHome" in driver.current_url:
                pass # Already logged in (rare)
            else:
                # Solve captcha
                captcha_img = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "captcha")))
                img_base64 = captcha_img.screenshot_as_base64
                
                # We attempt to solve using ddddocr
                result = ''
                try:
                    img_bytes = base64.b64decode(img_base64)
                    res = ocr.classification(img_bytes)
                    if res: result = res.replace(' ', '')
                except:
                    pass
                    
                if not result:
                    send_state_to_admin('waiting_for_captcha', 'OCR failed. Please solve captcha manually.', True, 'captcha', image_base64=img_base64)
                    result = wait_for_admin_input(180)
                    if not result:
                        driver.quit()
                        continue
                        
                send_state_to_admin('running', f'Submitting login (Captcha solved)...')
                user_field = driver.find_element(By.ID, "username")
                pass_field = driver.find_element(By.ID, "password")
                captcha_field = driver.find_element(By.ID, "captchaid")
                
                user_field.clear()
                user_field.send_keys(rtis_user)
                pass_field.clear()
                pass_field.send_keys(rtis_pass)
                captcha_field.clear()
                captcha_field.send_keys(result)
                
                if human_delay(0.5, 1.0): sys.exit(0)
                submit_btn = driver.find_element(By.XPATH, "//button[contains(text(), 'Login') or contains(@class, 'btn-primary')]")
                submit_btn.click()
                
                if human_delay(3, 5): sys.exit(0)

            # Check for invalid credentials/captcha
            src = driver.page_source
            if "Invalid Captcha" in src or "Captcha incorrect" in src:
                driver.quit()
                consecutive_captcha_failures += 1
                continue
            if "Invalid User Name" in src or "Invalid Credentials" in src:
                driver.quit()
                send_state_to_admin('error', 'RTIS User ID or Password incorrect. Please update settings.')
                if interruptible_sleep(60): sys.exit(0)
                continue

            # 3. OTP Page
            if "OTP" in driver.page_source or "MFAlogin" in driver.current_url or "verifyOTP" in driver.page_source or driver.find_elements(By.NAME, "otp"):
                # Extract mobile number if available
                phone_number = None
                try:
                    mobile_elem = driver.find_elements(By.ID, "mobileNewId")
                    if mobile_elem:
                        phone_number = mobile_elem[0].get_attribute("value")
                except Exception as e:
                    print(f"[RTIS] Error extracting phone number: {e}")

                send_state_to_admin('waiting_for_otp', 'OTP required. Please enter today\'s RTIS OTP.', True, 'otp', phone_number=phone_number)
                otp_val = None
                # Try to use today's OTP if we already saved it in this run
                otp_file = os.path.join(DATA_DIR, 'rtis_otp.txt')
                if os.path.exists(otp_file):
                    # check if modified today
                    mtime = os.path.getmtime(otp_file)
                    if datetime.datetime.fromtimestamp(mtime).date() == datetime.datetime.today().date():
                        with open(otp_file, 'r') as f:
                            otp_val = f.read().strip()
                
                if not otp_val:
                    otp_val = wait_for_admin_input(300)
                    if not otp_val:
                        driver.quit()
                        continue
                    # Save for today
                    with open(otp_file, 'w') as f: f.write(otp_val)
                    
                send_state_to_admin('running', 'Submitting OTP...')
                otp_field = driver.find_element(By.NAME, "otp")
                otp_field.clear()
                otp_field.send_keys(otp_val)
                if human_delay(0.5, 1.0): sys.exit(0)
                driver.find_element(By.XPATH, "//button[contains(text(), 'Submit OTP')]").click()
                if human_delay(3, 5): sys.exit(0)
                
                if "Invalid OTP" in driver.page_source:
                    if os.path.exists(otp_file): os.remove(otp_file)
                    driver.quit()
                    continue

            # Wait for dashboard to load (success)
            if "RTISDashboardUI/shed/shedHome" not in driver.current_url and "Dashboard" not in driver.page_source:
                # Still failing?
                send_state_to_admin('error', 'Failed to reach RTIS Dashboard after login/OTP.')
                driver.quit()
                interruptible_sleep(30)
                continue
                
            # 4. Extract Auth Info (JWT Token / JSESSIONID)
            send_state_to_admin('running', 'Extracting auth token...')
            
            bearer_token = None
            try:
                rtis_val = driver.find_element(By.ID, "RTIS").get_attribute("value")
                import json
                bearer_token = json.loads(rtis_val).get("accessToken")
            except Exception as e:
                print(f"[RTIS-DEBUG] Failed to get token from #RTIS input: {e}")
                # Fallback to local storage just in case
                bearer_token = driver.execute_script("return localStorage.getItem('token') || sessionStorage.getItem('token') || localStorage.getItem('jwtToken');")
                
            send_state_to_admin('running', f'Extracting auth token... {"Success" if bearer_token else "Failed!"}')
            print(f"[RTIS-DEBUG] Token retrieved: {str(bearer_token)[:50]}...", flush=True)
            
            cookies = driver.get_cookies()
            print(f"[RTIS-DEBUG] Cookies retrieved: {[c['name'] for c in cookies]}", flush=True)
            
            # Create requests session for API calls
            session = requests.Session()
            if proxy_host:
                session.proxies = {'http': f'socks5h://{proxy_host}:{proxy_port}', 'https': f'socks5h://{proxy_host}:{proxy_port}'}
            for c in cookies: session.cookies.set(c['name'], c['value'])
            
            headers = {
                'Accept': '*/*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Connection': 'keep-alive',
                'Content-Type': 'application/json',
                'Origin': 'https://rtis.indianrail.gov.in',
                'Referer': 'https://rtis.indianrail.gov.in/RTISDashboardUI/shed/divisionLiveLocoOnMap',
                'Sec-Fetch-Dest': 'empty',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Site': 'same-origin',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36'
            }
            if bearer_token:
                clean_token = str(bearer_token).replace('"', '')
                headers['Authorization'] = f"Bearer {clean_token}"

            # 5. Continuous Tracking Loop
            while True:
                if check_stop_signal():
                    driver.quit()
                    sys.exit(0)
                    
                # Fetch active locos from Webapp API
                send_state_to_admin('running', 'Fetching active locos from DB...')
                try:
                    resp = requests.get(f"http://127.0.0.1:{os.environ.get('PORT', 5000)}/api/rtis/active_locos", headers={'X-API-Secret': API_SECRET})
                    loco_data = resp.json()
                    active_locos = loco_data.get('locos', [])
                    loco_mapping = loco_data.get('mapping', {})
                except Exception as e:
                    print(f"[RTIS] Error fetching locos from app: {e}")
                    active_locos = []
                    
                if not active_locos:
                    send_state_to_admin('sleeping', 'No active locos to track. Sleeping...')
                else:
                    send_state_to_admin('running', f'Tracking locations for {len(active_locos)} locos...')
                    
                    # Track each loco
                    for loco in active_locos:
                        if check_stop_signal(): break
                        payload = {
                            "div": "",
                            "idList": [],
                            "locoNumber": loco,
                            "eventType": "",
                            "shedCode": "",
                            "flag": "L",
                            "filter": "Loco"
                        }
                        
                        try:
                            resp = session.post(
                                'https://rtis.indianrail.gov.in/RTISDashboard/LiveDataForDivision',
                                json=payload, headers=headers, timeout=15
                            )
                            if loco == active_locos[0]:
                                print(f"[RTIS-DEBUG] First loco {loco} response: {resp.status_code} - {resp.text[:300]}", flush=True)
                                
                            if resp.status_code == 200:
                                data = resp.json()
                                live_data = data.get('locoLiveData')
                                if live_data and isinstance(live_data, dict) and live_data.get('latitude') and live_data.get('longitude'):
                                    lat = float(live_data['latitude'])
                                    lon = float(live_data['longitude'])
                                    stn = live_data.get('locoStationCode', 'Unknown')
                                    
                                    conn2 = sqlite3.connect(os.path.join(DATA_DIR, 'crew.db'), timeout=10.0)
                                    prev_row = conn2.execute("SELECT latitude, longitude FROM loco_locations WHERE loco_no = ?", (loco,)).fetchone()
                                    conn2.close()
                                    
                                    moved_gt_100m = False
                                    if prev_row:
                                        prev_lat, prev_lon = prev_row
                                        if prev_lat is not None and prev_lon is not None:
                                            dist = haversine_distance(prev_lat, prev_lon, lat, lon)
                                            if dist > 100:
                                                moved_gt_100m = True
                                            
                                    crew_id = loco_mapping.get(loco)
                                    sync_payload = {
                                        "loco_no": loco,
                                        "crew_id": crew_id,
                                        "latitude": lat,
                                        "longitude": lon,
                                        "location_name": stn,
                                        "moved_gt_100m": moved_gt_100m
                                    }
                                    requests.post(f"http://127.0.0.1:{os.environ.get('PORT', 5000)}/api/rtis/sync_loco", 
                                                json=sync_payload, headers={'X-API-Secret': API_SECRET})
                        except Exception as e:
                            print(f"[RTIS] Error querying loco {loco}: {e}")
                        
                        if human_delay(1, 3): break

                requests.post(f"{FLASK_API_URL}/state", json={'last_run': datetime.datetime.now(IST).strftime('%d/%m/%y %H:%M:%S IST')}, headers={'X-API-Secret': API_SECRET})
                
                sleep_seconds = random_cycle_sleep_seconds(12, 18)
                send_state_to_admin('sleeping', f'Sync cycle complete. Sleeping for {sleep_seconds/60:.1f} minutes...')
                
                if interruptible_sleep(sleep_seconds):
                    break
                    
            driver.quit()
        except Exception as e:
            traceback.print_exc()
            send_state_to_admin('error', f'RTIS Scraper Error: {type(e).__name__} - {str(e)}')
            try: driver.quit()
            except: pass
            if interruptible_sleep(60): sys.exit(0)

if __name__ == '__main__':
    main_loop()
