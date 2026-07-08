import os, time, datetime, base64, io, json, traceback
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from PIL import Image
from google.cloud import vision

# Setup Paths & APIs
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, 'data')
if 'GCP_CREDENTIALS_B64' in os.environ:
    creds_json = base64.b64decode(os.environ['GCP_CREDENTIALS_B64']).decode('utf-8')
    creds_path = os.path.join(DATA_DIR, 'gcp_creds.json')
    with open(creds_path, 'w') as f:
        f.write(creds_json)
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = creds_path
else:
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = os.path.join(BASE_DIR, 'scraper', 'algebraic-cycle-432817-r8-ae9fa17cac37.json')

COOKIES_FILE = os.path.join(DATA_DIR, 'cookies.json')
LAST_RUN_FILE = os.path.join(DATA_DIR, 'last_run.txt')

FLASK_API_URL = f"http://127.0.0.1:{os.environ.get('PORT', 5000)}/api"
API_SECRET = os.environ.get('API_SECRET', 'cms-sync-secret-key-2026')

def send_state_to_admin(status, message, action_required=False, action_type='', image_base64=''):
    try:
        payload = {
            'status': status,
            'message': message,
            'action_required': action_required,
            'action_type': action_type,
            'image_base64': image_base64
        }
        requests.post(f"{FLASK_API_URL}/scraper/state", json=payload, headers={'X-API-Secret': API_SECRET})
    except Exception as e:
        print(f"Failed to communicate with Admin API: {e}")

def get_admin_action():
    try:
        resp = requests.get(f"{FLASK_API_URL}/scraper/action", headers={'X-API-Secret': API_SECRET})
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
        resp = requests.post(f"{FLASK_API_URL}/sync", json=current_crew_map, headers={'X-API-Secret': API_SECRET})
        if resp.status_code == 200:
            print("Successfully synced with Flask API")
            now = datetime.datetime.now()
            with open(LAST_RUN_FILE, 'w') as lf: lf.write(now.strftime('%d-%m-%Y %H:%M'))
            return True
    except Exception as e:
        print(f"Sync API failed: {e}")
        
    return False

# -----------------------------------------------------------------------------
# Main Loop
# -----------------------------------------------------------------------------

def main_loop():
    consecutive_captcha_failures = 0
    client = vision.ImageAnnotatorClient()
    
    while True:
        try:
            send_state_to_admin('starting', 'Checking saved cookies...')
            # 1. Try saved cookies
            cookie_valid = False
            session = requests.Session()
            session.proxies = {
                'http': 'socks5h://127.0.0.1:1080',
                'https': 'socks5h://127.0.0.1:1080',
            }
            if os.path.exists(COOKIES_FILE):
                with open(COOKIES_FILE, 'r') as cf:
                    saved_cookies = json.load(cf)
                for cookie in saved_cookies:
                    session.cookies.set(cookie['name'], cookie['value'])
                cookie_valid = sync_signon_reports(session)
            
            if cookie_valid:
                send_state_to_admin('sleeping', 'Sync successful. Sleeping for 30 minutes...')
                time.sleep(1800) # Sleep 30 mins after successful sync
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
            options.add_argument('--proxy-server=socks5://127.0.0.1:1080')

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
            time.sleep(3)
            
            # Handle Session Expire
            if "LOCAL COMPUTER WAS NOT USED" in driver.page_source or "Session Expire Page" in driver.title:
                try:
                    driver.execute_script("if (typeof load === 'function') { load(); } else { document.reportLoginForm.submit(); }")
                    time.sleep(3)
                    handle_alert(driver)
                except Exception:
                    pass
            
            # Solve Captcha
            try:
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
                # Auto OCR
                send_state_to_admin('running', 'Solving Captcha via OCR...')
                image_v = vision.Image(content=img_data)
                response = client.text_detection(image=image_v)
                texts = response.text_annotations
                if texts and texts[0].description:
                    for char in texts[0].description:
                        if char != " ":
                            result += char
                            if len(result) == 5: break
                result = result.lower()
            
            # Fetch CMS Credentials from DB
            cms_user = 'KGPCLIHQ'
            cms_pass = 'Cms@852'
            try:
                import sqlite3
                conn = sqlite3.connect(os.path.join(DATA_DIR, 'crew.db'))
                cur = conn.cursor()
                cur.execute('SELECT cms_username, cms_password FROM cms_settings WHERE id = 1')
                row = cur.fetchone()
                if row:
                    cms_user, cms_pass = row
                conn.close()
            except Exception as e:
                print(f"Error reading CMS credentials: {e}")

            # Login
            try:
                driver.find_element(By.ID, "User-Id").send_keys(cms_user)
                driver.find_element(By.ID, "user-Password").send_keys(cms_pass)
                driver.find_element(By.ID, "jcaptcha").send_keys(result)
                login_form = driver.find_element(By.ID, "loginForm")
                submit_buttons = login_form.find_elements(By.XPATH, ".//input[@type='submit' or @type='button']")
                if submit_buttons: submit_buttons[0].click()
                else: login_form.submit()
            except:
                driver.quit()
                continue
                
            time.sleep(3)
            handle_alert(driver)
            
            # Check Result
            source = driver.page_source
            if "Oops!" in source or "Error Code" in source:
                driver.quit()
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
                else:
                    consecutive_captcha_failures += 1
                    driver.quit()
                    continue
            else:
                consecutive_captcha_failures = 0
                
                otp_val = None
                OTP_FILE = os.path.join(DATA_DIR, 'otp.txt')
                if os.path.exists(OTP_FILE):
                    with open(OTP_FILE, 'r') as f:
                        otp_val = f.read().strip()
                        
                if not otp_val:
                    # OTP Flow
                    send_state_to_admin('waiting_for_otp', 'OTP required. Please check registered mobile.', True, 'otp')
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
                for i, digit in enumerate(otp_val):
                    if i < len(otp_inputs):
                        otp_inputs[i].send_keys(digit)
                        
                verify_btn = driver.find_element(By.XPATH, "//button[contains(text(), 'Verify') or contains(@onclick, 'verifyOTP')]")
                verify_btn.click()
                time.sleep(3)
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
                driver.execute_script("arguments[0].click();", skip_button)
                time.sleep(3)
            except:
                pass

            # Save Cookies
            new_cookies = driver.get_cookies()
            with open(COOKIES_FILE, 'w') as cf:
                json.dump(new_cookies, cf)
            
            # Sync
            session = requests.Session()
            session.proxies = {
                'http': 'socks5h://127.0.0.1:1080',
                'https': 'socks5h://127.0.0.1:1080',
            }
            for cookie in new_cookies:
                session.cookies.set(cookie['name'], cookie['value'])
            
            if sync_signon_reports(session):
                send_state_to_admin('sleeping', 'Sync successful. Sleeping for 30 minutes...')
                driver.quit()
                time.sleep(1800)
            else:
                driver.quit()

        except Exception as e:
            traceback.print_exc()
            send_state_to_admin('error', f'Exception: {e}')
            try: driver.quit()
            except: pass
            time.sleep(60)

if __name__ == '__main__':
    main_loop()
