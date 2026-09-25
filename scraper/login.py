import os, time, datetime, base64, io, json, traceback, zoneinfo, socket, random
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from PIL import Image
try:
    from google.cloud import vision
except ImportError:
    vision = None   # GCP Vision OCR is optional; ddddocr is the local fallback

# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
# Human-like timing helpers
# -----------------------------------------------------------------------------
import sys

def check_stop_signal():
    stop_file = os.path.join(DATA_DIR, 'stop.txt')
    return os.path.exists(stop_file)

def check_run_now_signal():
    """Returns True and deletes the signal file if a run-now was requested."""
    run_now_file = os.path.join(DATA_DIR, 'run_now.txt')
    if os.path.exists(run_now_file):
        try:
            os.remove(run_now_file)
        except OSError:
            pass
        return True
    return False

def interruptible_sleep(sleep_seconds):
    """Sleeps for sleep_seconds, but checks for stop/run-now signals every second.
    Returns True if a hard stop was signalled (caller should exit).
    Returns False on normal completion OR on run-now signal (caller should start a new cycle).
    """
    for _ in range(int(sleep_seconds)):
        if check_stop_signal():
            return True  # Hard stop — caller should exit
        if check_run_now_signal():
            return False  # Wake up early — start a new sync cycle
        time.sleep(1)
    if sleep_seconds > int(sleep_seconds):
        time.sleep(sleep_seconds - int(sleep_seconds))
    return check_stop_signal()

def human_delay(min_s, max_s):
    """Sleep a random amount between min_s and max_s seconds, interruptible."""
    return interruptible_sleep(random.uniform(min_s, max_s))

def human_type(element, text, min_delay=0.06, max_delay=0.18):
    """Type text character-by-character with small random delays,
    like a real person typing rather than Selenium's instant send_keys."""
    for ch in text:
        if check_stop_signal():
            return True
        element.send_keys(ch)
        time.sleep(random.uniform(min_delay, max_delay))
    return False

def random_cycle_sleep_seconds(min_minutes=25, max_minutes=35):
    """Random sleep between min_minutes and max_minutes, in seconds."""
    return random.uniform(min_minutes * 60, max_minutes * 60)


# Setup Paths & APIs
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, 'data')
def _decode_gcp_b64(env_var_name):
    """Decode a base64-encoded GCP JSON credential from an env var, handling encoding quirks."""
    b64_str = os.environ[env_var_name].strip()
    b64_str += "=" * ((4 - len(b64_str) % 4) % 4)
    raw_bytes = base64.b64decode(b64_str)
    # Strip BOM if present
    if raw_bytes[:3] == b'\xef\xbb\xbf':  raw_bytes = raw_bytes[3:]   # UTF-8 BOM
    if raw_bytes[:2] in (b'\xff\xfe', b'\xfe\xff'):  # UTF-16 BOM
        return raw_bytes.decode('utf-16')
    for enc in ('utf-8', 'utf-8-sig', 'utf-16', 'latin-1'):
        try:
            return raw_bytes.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw_bytes.decode('utf-8', errors='replace')  # absolute last resort

GCP_CREDENTIALS = []
# Option 1: Load up to 10 base64 environment variables (GCP_CREDENTIALS_B64_1, etc.)
for i in range(1, 11):
    env_var = f'GCP_CREDENTIALS_B64_{i}'
    if env_var in os.environ:
        creds_json = _decode_gcp_b64(env_var)
        creds_path = os.path.join(DATA_DIR, f'gcp_creds_{i}.json')
        with open(creds_path, 'w', encoding='utf-8') as f:
            f.write(creds_json)
        try: account_name = json.loads(creds_json).get('project_id', f'account_{i}')
        except: account_name = f'account_{i}'
        GCP_CREDENTIALS.append({'path': creds_path, 'name': account_name})

# Fallbacks if no numbered variables exist
if not GCP_CREDENTIALS:
    if 'GCP_CREDENTIALS_B64' in os.environ:
        creds_json = _decode_gcp_b64('GCP_CREDENTIALS_B64')
        creds_path = os.path.join(DATA_DIR, 'gcp_creds.json')
        with open(creds_path, 'w', encoding='utf-8') as f:
            f.write(creds_json)
        try: account_name = json.loads(creds_json).get('project_id', 'legacy_account')
        except: account_name = 'legacy_account'
        GCP_CREDENTIALS.append({'path': creds_path, 'name': account_name})
    else:
        print("[CMS] WARNING: No GCP_CREDENTIALS_B64 or GCP_CREDENTIALS_B64_N environment variable set. "
              "Google Vision OCR tier will be unavailable. CAPTCHA fallback will use ddddocr only.", flush=True)

COOKIES_FILE = os.path.join(DATA_DIR, 'cookies.json')
LAST_RUN_FILE = os.path.join(DATA_DIR, 'last_run.txt')

FLASK_API_URL = f"http://127.0.0.1:{os.environ.get('PORT', 5000)}/api"
API_SECRET = os.environ.get('API_SECRET')
if not API_SECRET:
    raise RuntimeError("FATAL: API_SECRET environment variable is not set. Scraper startup aborted.")

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

ACTIVE_TUNNEL = "Unknown"

def get_active_proxy():
    """Return SOCKS proxy host and port if available, checking Office PC first, then Laptop."""
    global ACTIVE_TUNNEL
    host = os.environ.get("SOCKS_PROXY_HOST", "127.0.0.1")
    
    print("[Proxy] Testing Primary Tunnel (Office PC) on port 1080...", flush=True)
    if is_proxy_available(host, 1080):
        print("[Proxy] Using Office PC proxy on port 1080.", flush=True)
        ACTIVE_TUNNEL = "Office PC (1080)"
        return host, 1080
        
    print("[Proxy] Primary tunnel failed. Testing Fallback Tunnel (Laptop) on port 1081...", flush=True)
    if is_proxy_available(host, 1081):
        print("[Proxy] Using Laptop proxy on port 1081.", flush=True)
        ACTIVE_TUNNEL = "Laptop (1081)"
        return host, 1081
        
    if os.environ.get('ENV', 'local').lower() == 'production':
        print("[Proxy] WARNING: Both tunnels are down. Production environment detected. Blocking direct connection.", flush=True)
        ACTIVE_TUNNEL = "Offline"
        return None, None
        
    print("[Proxy] WARNING: Both tunnels are down. Local environment detected. Attempting direct connection...", flush=True)
    ACTIVE_TUNNEL = "Direct (No Proxy)"
    return None, None

def make_session_with_proxy(cookies=None):
    """Create a requests.Session configured with the best available proxy.
    Optionally restore cookies from a list of {name, value} dicts."""
    proxy_host, proxy_port = get_active_proxy()
    session = requests.Session()
    if proxy_host:
        session.proxies = {
            'http': f'socks5h://{proxy_host}:{proxy_port}',
            'https': f'socks5h://{proxy_host}:{proxy_port}',
        }
    if cookies:
        for c in cookies:
            session.cookies.set(c['name'], c['value'])
    return session, proxy_host, proxy_port

def is_proxy_available(host, port, retries=2, delay=2):
    """Check if the SOCKS proxy port is open AND can actually reach the internet."""
    # Step 1: Check if the SOCKS proxy port is reachable
    port_open = False
    for attempt in range(retries):
        try:
            with socket.create_connection((host, port), timeout=2):
                port_open = True
                break
        except OSError:
            if attempt < retries - 1:
                time.sleep(delay)
    if not port_open:
        print(f"[Proxy] SOCKS proxy {host}:{port} not reachable.")
        return False
    
    # Step 2: Test actual internet connectivity through the proxy
    try:
        test_session = requests.Session()
        test_session.proxies = {
            'http': f'socks5h://{host}:{port}',
            'https': f'socks5h://{host}:{port}',
        }
        resp = test_session.get('https://cms.indianrail.gov.in', timeout=10)
        if "SignerDigitalExtPopupModal" in resp.text:
            raise Exception("Fallback page detected (CMS down or unreachable on this tunnel)")
        
        print(f"[Proxy] SOCKS proxy {host}:{port} is working. Using proxy.")
        return True
    except Exception as e:
        print(f"[Proxy] SOCKS proxy {host}:{port} port is open but internet test failed: {e}")
        return False

def send_state_to_admin(status, message, action_required=False, action_type='', image_base64='', last_ddddocr_failure=None, phone_number=None):
    global ACTIVE_TUNNEL
    try:
        payload = {
            'status': status,
            'message': message,
            'action_required': action_required,
            'action_type': action_type,
            'image_base64': image_base64,
            'active_tunnel': ACTIVE_TUNNEL
        }
        if phone_number:
            payload['phone_number'] = phone_number
        if last_ddddocr_failure:
            payload['last_ddddocr_failure'] = last_ddddocr_failure
        requests.post(f"{FLASK_API_URL}/scraper/state", json=payload, headers={'X-API-Secret': API_SECRET}, timeout=5)
    except Exception as e:
        print(f"Failed to communicate with Admin API: {e}")

def get_admin_action():
    try:
        resp = requests.get(f"{FLASK_API_URL}/scraper/action", headers={'X-API-Secret': API_SECRET}, timeout=5)
        if resp.status_code == 200:
            return resp.json().get('submitted_value')
    except Exception:
        pass
    return None

def wait_for_admin_input(timeout_seconds=300):
    start_time = time.time()
    while time.time() - start_time < timeout_seconds:
        val = get_admin_action()
        if val:
            return val
        time.sleep(2)
    return None

def handle_alert(driver):
    try:
        WebDriverWait(driver, 5).until(EC.alert_is_present(), 'Alert not present')
        alert = driver.switch_to.alert
        msg = alert.text
        alert.accept()
        print(f"Alert accepted: {msg}")
        return True
    except:
        return False

# -----------------------------------------------------------------------------
# Reporting Logic
# -----------------------------------------------------------------------------

def parse_drilldown_html(html_content, category):
    soup = BeautifulSoup(html_content, 'html.parser')
    table = soup.find('table', id='SignOnXHoursRunningTableDrillDown')
    if not table: return []
    tbody = table.find('tbody')
    if not tbody: return []
    records = []
    for row in tbody.find_all('tr'):
        cols = row.find_all('td')
        if len(cols) < 12: continue
        records.append({
            'crew_id': cols[1].text.strip(),
            'name': cols[2].text.strip(),
            'desig': cols[3].text.strip(),
            'from_sttn': cols[4].text.strip(),
            'sign_on_time': cols[5].text.strip(),
            'to_sttn': cols[6].text.strip(),
            'sign_off_time': cols[7].text.strip(),
            'duty_hrs': cols[8].text.strip(),
            'route': cols[9].text.strip(),
            'loco_no': cols[10].text.strip(),
            'train_no': cols[11].text.strip(),
            'category': category
        })
    return records

def sync_signon_reports(session_obj):
    send_state_to_admin('running', 'Initializing report session...')
    init_url = "https://cms.indianrail.gov.in/CMSREPORT/JSP/rpt/ReportHomePage.do"
    init_params = {
        'hmode': 'Filter', 'Lobby': 'SER-KGP-KGP',
        'actionURL': '../../JSP/rpt/running/SignOnXHrs.do?hmode=SignOnXHrs',
        'reportUsageLogURL': '../../JSP/rpt/ReportHomePage.do?hmode=Filter',
        'colDesigList': 'colDesigList', 'colSlot': 'false', 'colTraction': 'false',
        'colCrewDesig': 'false', 'colFlexi': 'false', 'ReportName': 'Current Sign On'
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html, */*; q=0.01', 'X-Requested-With': 'XMLHttpRequest',
        'Referer': 'https://cms.indianrail.gov.in/CMSREPORT/JSP/rpt/LoginAction.do?hmode=skipMapHrmsId&isResponsive=Y'
    }
    
    resp_init = session_obj.get(init_url, params=init_params, headers=headers, timeout=30)
    resp_text_lower = resp_init.text.lower()
    if "session expire page" in resp_text_lower or "loginform" in resp_text_lower or "jcaptcha" in resp_text_lower:
        return False

    main_url = "https://cms.indianrail.gov.in/CMSREPORT/JSP/rpt/running/SignOnXHrs.do?hmode=SignOnXHrs"
    xml_payload = """<?xml version="1.0" encoding="UTF-8"?><CMSPublishXML baseLanguage="string" transLanguage="string"><CMSREPORT action="REP" relationship="string" transLanguage="string"><zone>SER</zone><division>KGP</division><lobby>KGP</lobby><currentReportName><![CDATA[Current Sign On]]></currentReportName><desig1>false</desig1><desig2>false</desig2><desig3>LPG</desig3><desig4>false</desig4><desig5>false</desig5><desig6>false</desig6><desig7>false</desig7><desig8>false</desig8><desig9>false</desig9><desig10>false</desig10><desig11>false</desig11><desig12>false</desig12><abnormality1>COMMERCIAL</abnormality1><abnormality2>false</abnormality2><abnormality3>false</abnormality3><abnormality4>false</abnormality4><abnormality5>false</abnormality5><abnormality6>false</abnormality6><abnormality7>false</abnormality7><abnormality8>false</abnormality8><abnormality9>false</abnormality9><abnormality10>false</abnormality10><abnormality11>false</abnormality11><abnormality12>false</abnormality12><abnormality13>false</abnormality13><startingDate></startingDate><endDate></endDate><monthYearDateFormat></monthYearDateFormat><msgSrc>CS</msgSrc><traction>ALL</traction><cadre>E','M','B</cadre><fghtCochSht>FghtCoch</fghtCochSht><designation>PILOT</designation><active>Active</active><rlevel>LOBBY</rlevel><durationtype>FORTNIGHT</durationtype><combALP>COMB</combALP><slotData>not Slot Data</slotData><desigSelect>OFFICIATING</desigSelect><crewAvailableCheckList>CrewAvailableFIFO</crewAvailableCheckList><contValue>Continuous</contValue><mandatoryRequirementDueFilter>Reft</mandatoryRequirementDueFilter><signOnOFFVal>SignOnVal</signOnOFFVal><locoTraction>ALL</locoTraction><cont_NoncontValue>ContinuousHQ</cont_NoncontValue><contValueOption>SignOnOff</contValueOption><spare>spare</spare><crewHqSignOffSttn>asPerCrewHq</crewHqSignOffSttn><crewBAStatus>SIGNON</crewBAStatus><crewhq>HQ crew at HQ</crewhq><crewIDBaseID>CrewID</crewIDBaseID><crewDesgLevel>Goods</crewDesgLevel><currentMidnignt>CURRENT</currentMidnignt><abnormalityStatus>PN</abnormalityStatus><cadreFilter>notCadre</cadreFilter><year1></year1><crewBookingWrWorWise>CallBookLobbyWise</crewBookingWrWorWise><month1></month1><slotFilter>Previous</slotFilter><preodicCoursesVal>DONE</preodicCoursesVal><detailLevel>Detail</detailLevel><locoGroupVal>ELEC-CONV</locoGroupVal><time>4</time><dutyType>WR</dutyType><locoTypeWiseVal>Group</locoTypeWiseVal><reportGroupVal>Lobby</reportGroupVal><dfccRadio>ALL</dfccRadio><abnormalityNil>NOT_NIL</abnormalityNil><bmbsRadio>ALL</bmbsRadio><prRportType>realTime</prRportType><serviceTypeInput>ALL</serviceTypeInput><quizResultTypeVal>CategoryWise</quizResultTypeVal><fromSttn>null</fromSttn><toSttn>null</toSttn><slotValueCombo>Slot</slotValueCombo><locoNosearch></locoNosearch><monthCombo>Previous</monthCombo><monthComboValueText>Previous</monthComboValueText><indexValue>0</indexValue><slotValueText>Slot</slotValueText><route>- - -Route- - -</route><auCode>- - Select - -</auCode><weekDates>- - Select - -</weekDates><routename>- - Route- - -</routename><fromSttnNameRoute></fromSttnNameRoute><toSttnNameRoute></toSttnNameRoute><trainingValue>- - Select - -</trainingValue><trackCoverage>ALL</trackCoverage></CMSREPORT></CMSPublishXML>"""
    headers['Origin'] = 'https://cms.indianrail.gov.in'
    headers['Referer'] = 'https://cms.indianrail.gov.in/CMSREPORT/JSP/rpt/ReportHomePage.do?hmode=Filter'
    
    send_state_to_admin('running', 'Running main report...')
    resp_main = session_obj.post(main_url, data={'XML': xml_payload}, headers=headers, timeout=30)
    if resp_main.status_code != 200: return False

    categories = ['LT_4_HOURS', 'GE_4_HOURS_AND_LT6_HOURS', 'GE6_HOURS_AND_LT9_HOURS', 'GE_9_HOURS']
    current_crew_map = {}
    drilldown_url = "https://cms.indianrail.gov.in/CMSREPORT/JSP/rpt/running/SignOnXHrs.do"
    
    for cat in categories:
        send_state_to_admin('running', f'Fetching category {cat}...')
        drilldown_params = {'hmode': 'SignOnHrsDrillDown', 'divisionCode': 'KGP ', 'lobbyCode': '-', 'Desg': '[TOTAL]', 'signonHrs': cat, 'zoneCode': ''}
        resp_drill = session_obj.post(drilldown_url, params=drilldown_params, headers=headers, timeout=30)
        if resp_drill.status_code == 200:
            records = parse_drilldown_html(resp_drill.text, cat)
            for rec in records: current_crew_map[rec['crew_id']] = rec
        else:
            return False

    # POST to Flask API
    send_state_to_admin('running', 'Syncing data to backend DB...')
    try:
        resp = requests.post(f"{FLASK_API_URL}/sync", json=current_crew_map, headers={'X-API-Secret': API_SECRET}, timeout=30)
        if resp.status_code == 200:
            print("Successfully synced with Flask API")
            now = datetime.datetime.now(IST)
            with open(LAST_RUN_FILE, 'w') as lf: lf.write(now.strftime('%d-%m-%Y %H:%M'))
            return True
    except Exception as e:
        print(f"Sync API failed: {e}")
        
    return False

def sync_ta_reports(session_obj):
    send_state_to_admin('running', 'Fetching TA Crew Report...')
    
    now = datetime.datetime.now(IST)
    end_date_str = now.strftime('%d-%m-%Y %H:%M')
    start_date_str = (now - datetime.timedelta(days=1)).strftime('%d-%m-%Y %H:%M')
    
    xml_payload = f"""<?xml version="1.0" encoding="UTF-8"?><CMSPublishXML baseLanguage="string" transLanguage="string"><CMSREPORT action="REP" relationship="string" transLanguage="string"><zone>SER</zone><division>KGP</division><lobby>KGP</lobby><currentReportName><![CDATA[Booking On TA]]></currentReportName><desig1>false</desig1><desig2>false</desig2><desig3>LPG</desig3><desig4>false</desig4><desig5>false</desig5><desig6>false</desig6><desig7>false</desig7><desig8>false</desig8><desig9>false</desig9><desig10>false</desig10><desig11>false</desig11><desig12>false</desig12><abnormality1>COMMERCIAL</abnormality1><abnormality2>false</abnormality2><abnormality3>false</abnormality3><abnormality4>false</abnormality4><abnormality5>false</abnormality5><abnormality6>false</abnormality6><abnormality7>false</abnormality7><abnormality8>false</abnormality8><abnormality9>false</abnormality9><abnormality10>false</abnormality10><abnormality11>false</abnormality11><abnormality12>false</abnormality12><abnormality13>false</abnormality13><startingDate>{start_date_str}</startingDate><endDate>{end_date_str}</endDate><monthYearDateFormat></monthYearDateFormat><msgSrc>CS</msgSrc><traction>ALL</traction><cadre>E','M','B</cadre><fghtCochSht>FghtCoch</fghtCochSht><designation>PILOT</designation><active>Active</active><rlevel>DIVISION</rlevel><durationtype>FORTNIGHT</durationtype><combALP>COMB</combALP><slotData>not Slot Data</slotData><desigSelect>OFFICIATING</desigSelect><crewAvailableCheckList>CrewAvailableFIFO</crewAvailableCheckList><contValue>Continuous</contValue><mandatoryRequirementDueFilter>Reft</mandatoryRequirementDueFilter><signOnOFFVal>SignOnVal</signOnOFFVal><locoTraction>ALL</locoTraction><cont_NoncontValue>ContinuousHQ</cont_NoncontValue><contValueOption>SignOnOff</contValueOption><spare>spare</spare><crewHqSignOffSttn>asPerCrewHq</crewHqSignOffSttn><crewBAStatus>SIGNON</crewBAStatus><crewhq>HQ crew at HQ</crewhq><crewIDBaseID>CrewID</crewIDBaseID><crewDesgLevel>Goods</crewDesgLevel><currentMidnignt>CURRENT</currentMidnignt><abnormalityStatus>PN</abnormalityStatus><cadreFilter>notCadre</cadreFilter><year1></year1><crewBookingWrWorWise>CallBookLobbyWise</crewBookingWrWorWise><month1></month1><slotFilter>Previous</slotFilter><preodicCoursesVal>DONE</preodicCoursesVal><detailLevel>Detail</detailLevel><locoGroupVal>ELEC-CONV</locoGroupVal><time>4</time><dutyType>ALL</dutyType><locoTypeWiseVal>Group</locoTypeWiseVal><reportGroupVal>Lobby</reportGroupVal><dfccRadio>ALL</dfccRadio><abnormalityNil>NOT_NIL</abnormalityNil><bmbsRadio>ALL</bmbsRadio><prRportType>realTime</prRportType><serviceTypeInput>FREIGHT</serviceTypeInput><quizResultTypeVal>CategoryWise</quizResultTypeVal><fromSttn>null</fromSttn><toSttn>null</toSttn><slotValueCombo>Slot</slotValueCombo><locoNosearch></locoNosearch><monthCombo>Previous</monthCombo><monthComboValueText>Previous</monthComboValueText><indexValue>0</indexValue><slotValueText>Slot</slotValueText><route>- - -Route- - -</route><auCode>- - Select - -</auCode><weekDates>- - Select - -</weekDates><routename>- - Route- - -</routename><fromSttnNameRoute></fromSttnNameRoute><toSttnNameRoute></toSttnNameRoute><trainingValue>- - Select - -</trainingValue><trackCoverage>ALL</trackCoverage></CMSREPORT></CMSPublishXML>"""
    
    url = 'https://cms.indianrail.gov.in/CMSREPORT/JSP/rpt/crew/BookingOnTACrew.do?hmode=BookingOnTACrew'
    headers = {
        'Accept': 'text/html, */*; q=0.01',
        'Origin': 'https://cms.indianrail.gov.in',
        'Referer': 'https://cms.indianrail.gov.in/CMSREPORT/JSP/rpt/LoginAction.do?hmode=skipMapHrmsId&isResponsive=Y',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'
    }
    
    data = {'XML': xml_payload, 'lobby': 'KGP', 'lobbyList': 'SER-KGP-KGP'}
    try:
        resp = session_obj.post(url, headers=headers, data=data, timeout=30)
        if resp.status_code == 200:
            resp_text_lower = resp.text.lower()
            # NEW: Detect a stale/expired session the same way sync_signon_reports does.
            # Without this, an expired-cookie response (login page / session-expire page)
            # would silently fail the `table` lookup below and just return False,
            # making it look like "no TA data" instead of "session invalid".
            if "session expire page" in resp_text_lower or "loginform" in resp_text_lower or "jcaptcha" in resp_text_lower:
                print("TA Sync: session expired/invalid (login page returned)")
                return False

            soup = BeautifulSoup(resp.text, 'html.parser')
            table = soup.find('table', id='BookingOnTATable')
            if not table:
                print("TA Sync: 'BookingOnTATable' not found in response")
                return False
            tbody = table.find('tbody')
            if not tbody:
                print("TA Sync: table found but no <tbody>")
                return False
            
            ta_records = []
            for row in tbody.find_all('tr'):
                cols = row.find_all('td')
                if len(cols) < 25: continue
                
                # In the provided HTML structure:
                # [5] ORDERING TIME
                # [15] CREW ID
                # [24] MOBILE NO
                crew_id = cols[15].text.strip()
                if not crew_id: continue
                ordering_time = cols[5].text.strip()
                mobile_no = cols[24].text.strip()
                
                ta_records.append({
                    'crew_id': crew_id,
                    'ordering_time': ordering_time,
                    'mobile_number': mobile_no
                })
            
            if ta_records:
                send_state_to_admin('running', 'Syncing TA data to backend DB...')
                sync_resp = requests.post(f"{FLASK_API_URL}/sync_ta", json=ta_records, headers={'X-API-Secret': API_SECRET}, timeout=15)
                if sync_resp.status_code == 200:
                    print(f"Successfully synced TA data for {len(ta_records)} crews")
                    return True
                else:
                    # NEW: log non-200 responses instead of silently falling through to `return False`
                    print(f"TA Sync API returned {sync_resp.status_code}: {sync_resp.text[:300]}")
            else:
                # NEW: distinguish "table found but genuinely zero rows" from other failure paths
                print("TA Sync: table found but produced 0 records (0 rows matched column-count filter, or all crew_id blank)")
    except Exception as e:
        print(f"TA Sync failed: {e}")
        
    return False

def sync_cli_movements(session_obj):
    """Fetch CLI Movement Dashboard data and sync to backend."""
    send_state_to_admin('running', 'Fetching CLI Movement Dashboard...')

    # Build XML payload for CLI Movement Dashboard
    xml_payload = f"""<?xml version="1.0" encoding="UTF-8"?><CMSPublishXML baseLanguage="string" transLanguage="string"><CMSREPORT action="REP" relationship="string" transLanguage="string"><zone>SER</zone><division>KGP</division><lobby>KGP</lobby><currentReportName><![CDATA[CLI Movement Dashboard]]></currentReportName><desig1>false</desig1><desig2>false</desig2><desig3>LPG</desig3><desig4>false</desig4><desig5>false</desig5><desig6>false</desig6><desig7>false</desig7><desig8>false</desig8><desig9>false</desig9><desig10>false</desig10><desig11>false</desig11><desig12>false</desig12><abnormality1>COMMERCIAL</abnormality1><abnormality2>false</abnormality2><abnormality3>false</abnormality3><abnormality4>false</abnormality4><abnormality5>false</abnormality5><abnormality6>false</abnormality6><abnormality7>false</abnormality7><abnormality8>false</abnormality8><abnormality9>false</abnormality9><abnormality10>false</abnormality10><abnormality11>false</abnormality11><abnormality12>false</abnormality12><abnormality13>false</abnormality13><startingDate></startingDate><endDate></endDate><monthYearDateFormat></monthYearDateFormat><msgSrc>CS</msgSrc><traction>ALL</traction><cadre>E','M','B</cadre><fghtCochSht>FghtCoch</fghtCochSht><designation>PILOT</designation><active>Active</active><rlevel>LOBBY</rlevel><durationtype>FORTNIGHT</durationtype><combALP>COMB</combALP><slotData>not Slot Data</slotData><desigSelect>OFFICIATING</desigSelect><crewAvailableCheckList>CrewAvailableFIFO</crewAvailableCheckList><contValue>Continuous</contValue><mandatoryRequirementDueFilter>Reft</mandatoryRequirementDueFilter><signOnOFFVal>SignOnVal</signOnOFFVal><locoTraction>ALL</locoTraction><cont_NoncontValue>ContinuousHQ</cont_NoncontValue><contValueOption>SignOnOff</contValueOption><spare>spare</spare><crewHqSignOffSttn>asPerCrewHq</crewHqSignOffSttn><crewBAStatus>SIGNON</crewBAStatus><crewhq>HQ crew at HQ</crewhq><crewIDBaseID>CrewID</crewIDBaseID><crewDesgLevel>Goods</crewDesgLevel><currentMidnignt>CURRENT</currentMidnignt><abnormalityStatus>PN</abnormalityStatus><cadreFilter>notCadre</cadreFilter><year1></year1><crewBookingWrWorWise>CallBookLobbyWise</crewBookingWrWorWise><month1></month1><slotFilter>Previous</slotFilter><preodicCoursesVal>DONE</preodicCoursesVal><detailLevel>Detail</detailLevel><locoGroupVal>ELEC-CONV</locoGroupVal><time>4</time><dutyType>WR</dutyType><locoTypeWiseVal>Group</locoTypeWiseVal><reportGroupVal>Lobby</reportGroupVal><dfccRadio>ALL</dfccRadio><abnormalityNil>NOT_NIL</abnormalityNil><bmbsRadio>ALL</bmbsRadio><prRportType>realTime</prRportType><serviceTypeInput>ALL</serviceTypeInput><quizResultTypeVal>CategoryWise</quizResultTypeVal><fromSttn>null</fromSttn><toSttn>null</toSttn><slotValueCombo>Slot</slotValueCombo><locoNosearch></locoNosearch><monthCombo>Previous</monthCombo><monthComboValueText>Previous</monthComboValueText><indexValue>0</indexValue><slotValueText>Slot</slotValueText><route>- - -Route- - -</route><auCode>- - Select - -</auCode><weekDates>- - Select - -</weekDates><routename>- - Route- - -</routename><fromSttnNameRoute></fromSttnNameRoute><toSttnNameRoute></toSttnNameRoute><trainingValue>- - Select - -</trainingValue><trackCoverage>ALL</trackCoverage></CMSREPORT></CMSPublishXML>"""

    url = 'https://cms.indianrail.gov.in/CMSREPORT/JSPRWD/rpt/management/LIMovementDashboard.do?hmode=LIMovementDashboard'
    headers = {
        'Accept': 'text/html, */*; q=0.01',
        'Origin': 'https://cms.indianrail.gov.in',
        'Referer': 'https://cms.indianrail.gov.in/CMSREPORT/JSP/rpt/LoginAction.do?hmode=skipMapHrmsId&isResponsive=Y',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'
    }

    data = {'XML': xml_payload, 'lobby': 'KGP', 'lobbyList': 'SER-KGP-KGP'}

    try:
        resp = session_obj.post(url, headers=headers, data=data, timeout=30)
        if resp.status_code == 200:
            resp_text_lower = resp.text.lower()
            # Detect stale/expired session
            if "session expire page" in resp_text_lower or "loginform" in resp_text_lower or "jcaptcha" in resp_text_lower:
                print("CLI Movement Sync: session expired/invalid (login page returned)")
                return False

            soup = BeautifulSoup(resp.text, 'html.parser')
            table = soup.find('table', id='liMovementDashboard')
            if not table:
                print("CLI Movement Sync: 'liMovementDashboard' table not found in response")
                return False

            tbody = table.find('tbody')
            if not tbody:
                print("CLI Movement Sync: table found but no <tbody>")
                return False

            cli_records = []
            for row in tbody.find_all('tr'):
                cols = row.find_all('td')
                if len(cols) < 19:
                    continue

                # Parse columns (0-indexed):
                # [0] S.No, [1] CLI ID, [2] Name, [3] HQ Code,
                # [4] Departure Entry Date/Time, [5] Departure Date/Time,
                # [6] From Station, [7] Via, [8] To Station,
                # [9] Arrival Entry Date/Time, [10] Arrival Date/Time,
                # [11] Train No, [12] Loco No, [13] Authority,
                # [14] Duty Type, [15] Service Type, [16] Purpose,
                # [17] Entry Mode, [18] Remarks

                cli_id = cols[1].text.strip()
                name = cols[2].text.strip()

                if not cli_id or not name:
                    continue

                # Extract from_station and to_station text content
                from_station_td = cols[6]
                to_station_td = cols[8]

                from_station_text = from_station_td.get_text(strip=True)
                to_station_text = to_station_td.get_text(strip=True)

                # Filter: discard rows where BOTH from and to contain "null"
                if "null" in from_station_text.lower() and "null" in to_station_text.lower():
                    continue

                # Extract lat/lon from title attributes
                from_lat, from_lon = 0.0, 0.0
                to_lat, to_lon = 0.0, 0.0

                from_title = from_station_td.get('title', '')
                if 'lat=' in from_title and 'lon=' in from_title:
                    try:
                        lat_part = from_title.split('lat=')[1].split('&')[0]
                        lon_part = from_title.split('lon=')[1]
                        from_lat = float(lat_part)
                        from_lon = float(lon_part)
                    except: pass

                to_title = to_station_td.get('title', '')
                if 'lat=' in to_title and 'lon=' in to_title:
                    try:
                        lat_part = to_title.split('lat=')[1].split('&')[0]
                        lon_part = to_title.split('lon=')[1]
                        to_lat = float(lat_part)
                        to_lon = float(lon_part)
                    except: pass

                cli_records.append({
                    'cli_id': cli_id,
                    'name': name,
                    'hq_code': cols[3].text.strip(),
                    'departure_entry_datetime': cols[4].text.strip(),
                    'departure_datetime': cols[5].text.strip(),
                    'from_station': from_station_text,
                    'via': cols[7].text.strip(),
                    'to_station': to_station_text,
                    'arrival_entry_datetime': cols[9].text.strip(),
                    'arrival_datetime': cols[10].text.strip(),
                    'train_no': cols[11].text.strip(),
                    'loco_no': cols[12].text.strip(),
                    'authority': cols[13].text.strip(),
                    'duty_type': cols[14].text.strip(),
                    'service_type': cols[15].text.strip(),
                    'purpose': cols[16].text.strip(),
                    'entry_mode': cols[17].text.strip(),
                    'remarks': cols[18].text.strip(),
                    'from_lat': from_lat,
                    'from_lon': from_lon,
                    'to_lat': to_lat,
                    'to_lon': to_lon
                })

            if cli_records:
                send_state_to_admin('running', 'Syncing CLI movement data to backend DB...')
                sync_resp = requests.post(
                    f"{FLASK_API_URL}/sync_cli_movements",
                    json=cli_records,
                    headers={'X-API-Secret': API_SECRET},
                    timeout=15
                )
                if sync_resp.status_code == 200:
                    print(f"Successfully synced CLI movement data for {len(cli_records)} CLIs")
                    return True
                else:
                    print(f"CLI Movement Sync API returned {sync_resp.status_code}: {sync_resp.text[:300]}")
            else:
                print("CLI Movement Sync: table found but produced 0 records after null filtering")

    except Exception as e:
        print(f"CLI Movement Sync failed: {e}")

    return False

# -----------------------------------------------------------------------------
# Main Loop
# -----------------------------------------------------------------------------

def main_loop():
    consecutive_captcha_failures = 0
    consecutive_auth_failures = 0
    
    active_client = None
    active_account_name = None
    last_rotation_date = None
    
    # Lazy-load ddddocr to avoid thread/fork deadlocks on startup
    print("Initializing ddddocr...")
    try:
        import ddddocr
        ocr = ddddocr.DdddOcr(show_ad=False)
        print("ddddocr initialized successfully.")
    except Exception as e:
        print(f"Failed to initialize ddddocr: {e}")
        ocr = None
    
    while True:
        current_date = datetime.datetime.now(IST).date()
        if GCP_CREDENTIALS and vision and last_rotation_date != current_date:
            active_index = current_date.toordinal() % len(GCP_CREDENTIALS)
            active_cred = GCP_CREDENTIALS[active_index]
            os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = active_cred['path']
            active_client = vision.ImageAnnotatorClient()
            active_account_name = active_cred['name']
            last_rotation_date = current_date
            print(f"[{current_date}] Switched to OCR account: {active_account_name}")
        elif not GCP_CREDENTIALS:
            # Explicitly clear client if we don't have GCP config
            active_client = None
            active_account_name = None

        if check_stop_signal():
            print("Stop signal detected. Exiting scraper process gracefully.")
            sys.exit(0)
            
        try:
            send_state_to_admin('starting', 'Checking saved cookies...')
            # 1. Try saved cookies — with tunnel-aware retry
            cookie_valid = False
            saved_cookies = []
            if os.path.exists(COOKIES_FILE):
                with open(COOKIES_FILE, 'r') as cf:
                    saved_cookies = json.load(cf)

            if saved_cookies:
                # Try up to twice: once with the best tunnel, once with a re-selected tunnel
                for tunnel_attempt in range(2):
                    session, proxy_host, proxy_port = make_session_with_proxy(saved_cookies)
                    if tunnel_attempt > 0:
                        send_state_to_admin('running', f'Tunnel switched to {ACTIVE_TUNNEL}, retrying sync...')
                    try:
                        cookie_valid = sync_signon_reports(session)
                        break  # success or session-expired — either way, stop retrying
                    except requests.exceptions.ConnectionError as ce:
                        print(f"[Proxy] ConnectionError on tunnel attempt {tunnel_attempt + 1}: {ce}")
                        send_state_to_admin('running', f'Tunnel {ACTIVE_TUNNEL} lost connectivity, re-testing tunnels...')
                        if tunnel_attempt == 0:
                            # Force a re-test by clearing any cached port-open state
                            time.sleep(3)
                            continue
                        else:
                            # Both attempts failed
                            send_state_to_admin('error', 'Error: Cannot connect to CMS Server on any tunnel. The SOCKS proxies might be offline or CMS is down.')
                            if interruptible_sleep(60):
                                sys.exit(0)
                            continue
            
            proxy_host, proxy_port = ACTIVE_TUNNEL, None  # already set by make_session_with_proxy

            # ---------------------------------------------------------------
            # FIX: sync_ta_reports() used to be called ONLY after a fresh
            # Selenium login (far below, in the login-flow branch). As long
            # as saved cookies stayed valid, the code always took this early
            # "cookie_valid" branch and looped back via `continue` WITHOUT
            # ever calling sync_ta_reports(). Since valid-cookie reuse is the
            # common case (that's the entire point of caching cookies), TA
            # sync was effectively dead code in practice -- confirmed by prod
            # logs showing /api/sync firing but /api/sync_ta never firing.
            #
            # Now TA sync runs on every cycle where the session is valid,
            # not just immediately after a fresh login.
            # ---------------------------------------------------------------
            if cookie_valid:
                ta_ok = sync_ta_reports(session)
                if not ta_ok:
                    print("[main_loop] TA sync did not succeed this cycle (see sync_ta_reports logs above).")

            if cookie_valid:
                cli_ok = sync_cli_movements(session)
                if not cli_ok:
                    print("[main_loop] CLI Movement sync did not succeed this cycle (see sync_cli_movements logs above).")

            if cookie_valid:
                sleep_seconds = random_cycle_sleep_seconds(25, 35)
                sleep_minutes = sleep_seconds / 60
                send_state_to_admin('sleeping', f'Sync successful. Sleeping for {sleep_minutes:.1f} minutes...')
                if interruptible_sleep(sleep_seconds):
                    sys.exit(0)
                continue
                
            # 2. Login Flow (Headless)
            send_state_to_admin('running', 'Launching headless browser for login...')
            options = Options()
            options.add_argument("--headless=new")
            options.add_argument("--window-size=1280,800")
            options.add_argument("--disable-gpu")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            # Low-memory flags — important for 1 GB RAM VMs
            options.add_argument("--disable-extensions")
            options.add_argument("--disable-background-networking")
            options.add_argument("--disable-sync")
            options.add_argument("--metrics-recording-only")
            options.add_argument("--mute-audio")
            options.add_argument("--no-first-run")
            options.add_argument("--safebrowsing-disable-auto-update")
            options.add_argument("--js-flags=--max-old-space-size=256")
            proxy_host, proxy_port = get_active_proxy()
            if not proxy_host and ACTIVE_TUNNEL == "Offline":
                send_state_to_admin('error', 'Error: Cannot connect to CMS Server. Production environment blocks direct connections.')
                if interruptible_sleep(60):
                    sys.exit(0)
                continue
            if proxy_host:
                options.add_argument(f'--proxy-server=socks5://{proxy_host}:{proxy_port}')

            # Detect Chromium binary path (Linux vs Windows)
            chrome_bin = os.environ.get('CHROME_BIN', '')
            if not chrome_bin:
                import shutil
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

            driver.get('https://cms.indianrail.gov.in/CMSREPORT/JSP/rpt/LoginAction.do?hmode=login&isResponsive=Y')
            human_delay(2.5, 5.0)  # time for the page to "be looked at" before doing anything
            
            if "SignerDigitalExtPopupModal" in driver.page_source:
                raise Exception("CMS_SERVER_DOWN")
            
            # Handle Session Expire
            if "LOCAL COMPUTER WAS NOT USED" in driver.page_source or "Session Expire Page" in driver.title:
                try:
                    if human_delay(0.8, 2.0): sys.exit(0)
                    driver.execute_script("if (typeof load === 'function') { load(); } else { document.reportLoginForm.submit(); }")
                    if human_delay(2.5, 4.5): sys.exit(0)
                    handle_alert(driver)
                except Exception:
                    pass
            
            # Solve Captcha
            try:
                human_delay(0.5, 1.5)  # brief pause before locating the captcha image
                img_el = driver.find_element(By.ID, "capt")
                src = img_el.get_attribute('src')
                b64_img = src.split(',')[1]
                img_data = base64.b64decode(b64_img)
            except:
                driver.quit()
                time.sleep(5)
                continue

            result = ""
            if consecutive_captcha_failures >= 10:
                # Ask Admin
                send_state_to_admin('waiting_for_captcha', 'Captcha failed 10 times. Please solve manually.', True, 'captcha', b64_img)
                print("Waiting for admin captcha...")
                result = wait_for_admin_input(300)
                if not result:
                    print("Admin timeout. Restarting flow.")
                    driver.quit()
                    continue
                consecutive_captcha_failures = 0 # reset on manual input
            else:
                if ocr and consecutive_captcha_failures < 5:
                    send_state_to_admin('running', f'Solving Captcha via ddddocr (Attempt {consecutive_captcha_failures + 1}/5)...')
                    try:
                        res = ocr.classification(img_data)
                        if res:
                            # ddddocr sometimes returns weird cases or extra spaces, clean it up
                            result = res.lower().replace(' ', '')[:5]
                    except Exception as e:
                        print(f"ddddocr failed: {e}")
                
                if not result and active_client and vision:
                    if consecutive_captcha_failures == 5 and ocr:
                        failure_time = datetime.datetime.now(IST).strftime('%d/%m/%y %H:%M:%S IST')
                        send_state_to_admin('running', f'ddddocr failed 5 times, falling back to Google Vision...', last_ddddocr_failure=failure_time)
                    else:
                        send_state_to_admin('running', f'Solving Captcha via OCR ({active_account_name})...')

                    image_v = vision.Image(content=img_data)
                    response = active_client.text_detection(image=image_v)
                    
                    # Report OCR usage to backend
                    try:
                        requests.post(f"{FLASK_API_URL}/ocr_usage", json={'account_name': active_account_name}, headers={'X-API-Secret': API_SECRET}, timeout=5)
                    except Exception as e:
                        print(f"Failed to report OCR usage: {e}")
                        
                    texts = response.text_annotations
                    if texts and texts[0].description:
                        for char in texts[0].description:
                            if char != " ":
                                result += char
                                if len(result) == 5: break
                    result = result.lower()
            
            cms_user, cms_pass = '', ''
            try:
                import sqlite3
                conn = sqlite3.connect(os.path.join(DATA_DIR, 'crew.db'), timeout=10.0)
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA busy_timeout=5000;")
                cur = conn.cursor()
                cur.execute('SELECT cms_username, cms_password FROM cms_settings WHERE id = 1')
                row = cur.fetchone()
                if row:
                    cms_user, cms_pass = row
                conn.close()
            except Exception as e:
                print(f"Error reading CMS credentials: {e}")

            if not cms_user or not cms_pass:
                print("[main_loop] No CMS credentials configured. Skipping login attempt.")
                send_state_to_admin('error', 'No CMS credentials configured in DB. Please set them in CMS Settings.')
                driver.quit()
                interruptible_sleep(30)
                continue

            # Login
            try:
                user_field = driver.find_element(By.ID, "User-Id")
                if human_delay(0.4, 1.1): sys.exit(0)
                user_field.click()
                if human_type(user_field, cms_user): sys.exit(0)

                if human_delay(0.3, 0.9): sys.exit(0)
                pass_field = driver.find_element(By.ID, "user-Password")
                pass_field.click()
                if human_type(pass_field, cms_pass): sys.exit(0)

                if human_delay(0.4, 1.0): sys.exit(0)
                captcha_field = driver.find_element(By.ID, "jcaptcha")
                captcha_field.click()
                if human_type(captcha_field, result): sys.exit(0)

                if human_delay(0.5, 1.3): sys.exit(0)
                login_form = driver.find_element(By.ID, "loginForm")
                submit_buttons = login_form.find_elements(By.XPATH, ".//input[@type='submit' or @type='button']")
                if submit_buttons: submit_buttons[0].click()
                else: login_form.submit()
            except:
                driver.quit()
                continue
                
            if human_delay(2.5, 4.5): sys.exit(0)
            handle_alert(driver)
            
            # Check Result
            source = driver.page_source
            
            # Look for specific error messages
            error_text = ""
            try:
                error_div = driver.find_element(By.XPATH, "/html/body/section/div/div[2]/div[3]/div")
                error_text = error_div.text.strip()
            except:
                pass

            if "User Id/Password Does Not Match" in error_text or "User Id/Password Does Not Match" in source:
                consecutive_auth_failures += 1
                if consecutive_auth_failures >= 2:
                    # Write fatal error to stop restarts
                    with open(os.path.join(DATA_DIR, 'fatal_error.txt'), 'w') as f:
                        f.write('auth_error')
                    send_state_to_admin('error', 'User Id/Password Does Not Match. Please update in CMS Settings and trigger manually.')
                    driver.quit()
                    sys.exit(1)
                else:
                    driver.quit()
                    continue
                    
            if "Please Enter Valid Captcha!" in error_text or "Please Enter Valid Captcha!" in source:
                consecutive_captcha_failures += 1
                driver.quit()
                continue

            if error_text and "Oops!" not in error_text and "Error Code" not in error_text:
                # Other specific error shown in the UI
                send_state_to_admin('error', f'Login Error: {error_text}')
                driver.quit()
                interruptible_sleep(10)
                continue

            if "Oops!" in source or "Error Code" in source:
                send_state_to_admin('error', 'CMS returned Oops! or Error Code. Retrying soon...')
                driver.quit()
                interruptible_sleep(30)
                continue
            
            otp_detected = False
            try:
                driver.find_element(By.CLASS_NAME, "otp-input")
                otp_detected = True
            except: pass

            if not otp_detected:
                if "MapHRMS" in source or "SKIP" in source or "MISC" in source:
                    # Bypassed OTP
                    consecutive_captcha_failures = 0
                    consecutive_auth_failures = 0
                else:
                    consecutive_captcha_failures += 1
                    driver.quit()
                    continue
            else:
                consecutive_captcha_failures = 0
                consecutive_auth_failures = 0
                
                otp_val = None
                OTP_FILE = os.path.join(DATA_DIR, 'otp.txt')
                if os.path.exists(OTP_FILE):
                    # Only reuse the saved OTP if it was written today (same pattern as rtis_otp.txt)
                    mtime = os.path.getmtime(OTP_FILE)
                    if datetime.datetime.fromtimestamp(mtime).date() == datetime.datetime.now(IST).date():
                        with open(OTP_FILE, 'r') as f:
                            otp_val = f.read().strip()
                        
                if not otp_val:
                    # OTP Flow
                    import re
                    phone_match = re.search(r'Enter OTP sent to \+91[X\*]*(\d{4})', source)
                    phone_number = f"Ending in {phone_match.group(1)}" if phone_match else None
                    send_state_to_admin('waiting_for_otp', 'OTP required. Please check registered mobile.', True, 'otp', phone_number=phone_number)
                    otp_val = wait_for_admin_input(300)
                    if not otp_val:
                        driver.quit()
                        continue
                    with open(OTP_FILE, 'w') as f:
                        f.write(otp_val)
                else:
                    send_state_to_admin('running', 'Using saved OTP for login...')
                
                # Enter OTP
                otp_inputs = driver.find_elements(By.CSS_SELECTOR, ".otp-input input")
                for inp in otp_inputs:
                    inp.send_keys(Keys.BACKSPACE)
                    inp.clear()
                if human_delay(0.5, 1.2): sys.exit(0)
                for i, digit in enumerate(otp_val):
                    if i < len(otp_inputs):
                        otp_inputs[i].send_keys(digit)
                        if human_delay(0.15, 0.45): sys.exit(0)

                if human_delay(0.4, 1.0): sys.exit(0)
                verify_btn = driver.find_element(By.XPATH, "//button[contains(text(), 'Verify') or contains(@onclick, 'verifyOTP')]")
                verify_btn.click()
                if human_delay(2.5, 4.5): sys.exit(0)
                handle_alert(driver)
                
                source = driver.page_source
                if "Hrms-Id" not in source and "SKIP" not in source and "MISC" not in source:
                    # Failed OTP
                    if os.path.exists(OTP_FILE):
                        os.remove(OTP_FILE)
                    driver.quit()
                    continue

            # SKIP Page handling
            try:
                skip_button = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.XPATH, "//input[@value='SKIP' or @value='Skip'] | //input[contains(@onclick, 'SKIP')] | //input[@name='hmode' and @value='SKIP']"))
                )
                if human_delay(0.6, 1.6): sys.exit(0)
                driver.execute_script("arguments[0].click();", skip_button)
                if human_delay(2.5, 4.0): sys.exit(0)
            except:
                pass

            # Save Cookies
            new_cookies = driver.get_cookies()
            with open(COOKIES_FILE, 'w') as cf:
                json.dump(new_cookies, cf)
            
            # Sync — with tunnel-aware retry (same as cookie path)
            signon_success = False
            ta_success = False
            for tunnel_attempt in range(2):
                session, proxy_host, proxy_port = make_session_with_proxy(new_cookies)
                if tunnel_attempt > 0:
                    send_state_to_admin('running', f'Tunnel switched to {ACTIVE_TUNNEL}, retrying sync after login...')
                try:
                    signon_success = sync_signon_reports(session)
                    ta_success = sync_ta_reports(session)
                    cli_success = sync_cli_movements(session)
                    break  # success — stop retrying
                except requests.exceptions.ConnectionError as ce:
                    print(f"[Proxy] Post-login ConnectionError on tunnel attempt {tunnel_attempt + 1}: {ce}")
                    send_state_to_admin('running', f'Tunnel {ACTIVE_TUNNEL} lost connectivity after login, re-testing tunnels...')
                    if tunnel_attempt == 0:
                        time.sleep(3)
                        continue
                    else:
                        send_state_to_admin('error', 'Error: Cannot connect to CMS Server on any tunnel. The SOCKS proxies might be offline or CMS is down.')
                        break

            if signon_success or ta_success or cli_success:
                sleep_seconds = random_cycle_sleep_seconds(25, 35)
                sleep_minutes = sleep_seconds / 60
                send_state_to_admin('sleeping', f'Sync successful. Sleeping for {sleep_minutes:.1f} minutes...')
                driver.quit()
                if interruptible_sleep(sleep_seconds):
                    sys.exit(0)
            else:
                driver.quit()

        except Exception as e:
            traceback.print_exc()
            err_str = str(e)
            if "ERR_CONNECTION_REFUSED" in err_str or "Connection refused" in err_str or "Max retries exceeded" in err_str:
                error_msg = "Error: Cannot connect to CMS Server. The SOCKS proxy might be offline or the CMS Server is completely down."
            elif "CMS_SERVER_DOWN" in err_str:
                error_msg = "Error: CMS Server is down or undergoing maintenance. (Signer.Digital fallback page detected)."
            else:
                error_msg = f'Code Error: {type(e).__name__} - {err_str}'
                
            send_state_to_admin('error', error_msg)
            try: driver.quit()
            except: pass
            if interruptible_sleep(60):
                sys.exit(0)

if __name__ == '__main__':
    main_loop()