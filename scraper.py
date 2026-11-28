import json
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta
import pytz
from playwright.sync_api import sync_playwright
import firebase_admin
from firebase_admin import credentials, db

# Đường dẫn Realtime Database
RTDB_URL = "https://sgn-flight-status-default-rtdb.asia-southeast1.firebasedatabase.app"

# 1. Khởi tạo kết nối Firebase Realtime Database
def init_firebase():
    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not sa_json:
        raise ValueError("Thiếu biến môi trường FIREBASE_SERVICE_ACCOUNT!")
    cred_dict = json.loads(sa_json)
    cred = credentials.Certificate(cred_dict)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred, {
            "databaseURL": RTDB_URL
        })

# Kiểm tra chéo: Giờ dự đáp (ETA) không thể sớm hơn giờ kế hoạch (STA) quá 45 phút
def is_valid_arrival_eta(eta_str, sta_str):
    if not eta_str or eta_str == "--" or not sta_str or sta_str == "--:--":
        return False
    try:
        eh, em = map(int, eta_str.split(":"))
        sh, sm = map(int, sta_str.split(":"))
        eta_mins = eh * 60 + em
        sta_mins = sh * 60 + sm
        diff = eta_mins - sta_mins
        if diff < -720:
            diff += 1440
        elif diff > 720:
            diff -= 1440
        return diff >= -45
    except Exception:
        return True

# TẦNG 1A: Quét bản đồ bầu trời
def get_airborne_flights_to_sgn():
    airborne_map = {}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Origin": "https://www.flightradar24.com",
        "Referer": "https://www.flightradar24.com/"
    }

    base_params = "faa=1&satellite=1&mlat=1&flarm=1&adsb=1&gnd=0&air=1&vehicles=0&estimated=1&maxage=14400&gliders=1&stats=0&limit=5000"
    feed_urls = [
        f"https://data-cloud.flightradar24.com/zones/fcgi/feed.js?{base_params}&to=SGN",
        f"https://data-cloud.flightradar24.com/zones/fcgi/feed.js?{base_params}&airline=HVN"
    ]

    for url in feed_urls:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    for f_id, info in data.items():
                        if not isinstance(info, list) or len(info) < 15:
                            continue
                        dest = str(info[12] or "").upper().strip()
                        on_ground = info[14]
                        altitude = info[4] if isinstance(info[4], (int, float)) else 0

                        if on_ground == 1 and altitude <= 0:
                            continue
                        if dest and dest != "SGN":
                            continue

                        iata_no = str(info[13] or "").upper().replace(" ", "").strip()
                        callsign = str(info[16] or "").upper().replace(" ", "").strip()

                        vn_no = ""
                        if iata_no.startswith("VN"):
                            vn_no = iata_no
                        elif callsign.startswith("HVN"):
                            vn_no = callsign.replace("HVN", "VN", 1)

                        if vn_no:
                            airborne_map[vn_no] = f_id
        except Exception:
            pass

    return airborne_map

# TẦNG 1B: Kiểm tra qua cổng Tìm Kiếm LIVE FLIGHTS của Flightradar24
def find_live_flight_id_via_search(flight_no):
    clean_no = flight_no.replace(" ", "").upper()
    callsign_no = clean_no.replace("VN", "HVN", 1) if clean_no.startswith("VN") else clean_no
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Origin": "https://www.flightradar24.com",
        "Referer": "https://www.flightradar24.com/"
    }

    for q in [clean_no, callsign_no]:
        try:
            url = f"https://www.flightradar24.com/v1/search/web/find?query={q}&limit=15"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=6) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    for item in data.get("results", []):
                        if item.get("type") == "live":
                            detail = item.get("detail", {})
                            schd_to = (detail.get("schd_to") or "").upper().strip()
                            f_code = (detail.get("flight") or "").upper().replace(" ", "").strip()
                            cs_code = (detail.get("callsign") or "").upper().replace(" ", "").strip()
                            label = (item.get("label") or "").upper().replace(" ", "").strip()

                            if (not schd_to or schd_to == "SGN") and (f_code == clean_no or cs_code == callsign_no or clean_no in label or callsign_no in label):
                                return item.get("id")
        except Exception:
            pass
    return None

# Lấy chính xác GIỜ DỰ ĐÁP (ETA) qua flight_id
def fetch_airborne_eta_by_id(flight_id, flight_no):
    tz_vn = pytz.timezone("Asia/Ho_Chi_Minh")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Origin": "https://www.flightradar24.com",
        "Referer": "https://www.flightradar24.com/"
    }

    try:
        url = f"https://data-live.flightradar24.com/clickhandler/?version=1.5&flight={flight_id}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                dest = (data.get("airport", {}).get("destination", {}).get("code", {}).get("iata") or "").upper()
                if dest != "SGN":
                    return "--", None

                status_obj = data.get("status", {})
                raw_status_text = (status_obj.get("text") or "").strip()
                status_upper = raw_status_text.upper()

                time_info = data.get("time", {})
                real_arr = time_info.get("real", {}).get("arrival")
                est_arr = time_info.get("estimated", {}).get("arrival")
                sched_arr = time_info.get("scheduled", {}).get("arrival")

                if real_arr is not None or "LANDED" in status_upper or "ARRIVED" in status_upper:
                    return "--", None

                arr_dt = None
                if sched_arr:
                    arr_dt = datetime.fromtimestamp(sched_arr, tz_vn)
                elif est_arr:
                    arr_dt = datetime.fromtimestamp(est_arr, tz_vn)

                if est_arr:
                    dt = datetime.fromtimestamp(est_arr, tz_vn)
                    return dt.strftime("%H:%M"), arr_dt

                if "ESTIMATED ARRIVAL" in status_upper:
                    match_arr = re.search(r"estimated\s+arrival\s*[:\s•]*(\d{1,2}:\d{2}\s*(?:AM|PM)?)", raw_status_text, re.I)
                    if match_arr:
                        t_str = match_arr.group(1).strip()
                        try:
                            t_obj = datetime.strptime(t_str, "%I:%M %p")
                            return t_obj.strftime("%H:%M"), arr_dt
                        except Exception:
                            return t_str, arr_dt
    except Exception:
        pass

    return "--", None

# TẦNG 3: Kiểm tra qua cổng list.json
def fetch_live_eta_via_list_api(flight_no):
    clean_no = flight_no.replace(" ", "").upper()
    callsign_no = clean_no.replace("VN", "HVN", 1) if clean_no.startswith("VN") else clean_no
    tz_vn = pytz.timezone("Asia/Ho_Chi_Minh")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Origin": "https://www.flightradar24.com",
        "Referer": "https://www.flightradar24.com/"
    }

    for q in [clean_no, callsign_no]:
        try:
            url = f"https://api.flightradar24.com/common/v1/flight/list.json?query={q}&fetchBy=flight&page=1&limit=5"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=6) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    for item in data.get("result", {}).get("response", {}).get("data", []):
                        dest = (item.get("airport", {}).get("destination", {}).get("code", {}).get("iata") or "").upper()
                        if dest != "SGN":
                            continue

                        status_obj = item.get("status", {})
                        is_live = bool(status_obj.get("live", False))
                        raw_status = (status_obj.get("text") or "").strip()
                        status_upper = raw_status.upper()

                        time_info = item.get("time", {})
                        real_dep = time_info.get("real", {}).get("departure")
                        real_arr = time_info.get("real", {}).get("arrival")
                        est_arr = time_info.get("estimated", {}).get("arrival")
                        sched_arr = time_info.get("scheduled", {}).get("arrival")

                        if real_arr is not None or "LANDED" in status_upper or "ARRIVED" in status_upper:
                            continue

                        if not (is_live or (real_dep is not None and real_arr is None) or "ESTIMATED ARRIVAL" in status_upper):
                            continue

                        arr_dt = datetime.fromtimestamp(sched_arr or est_arr, tz_vn) if (sched_arr or est_arr) else None

                        if est_arr:
                            dt = datetime.fromtimestamp(est_arr, tz_vn)
                            return dt.strftime("%H:%M"), arr_dt

                        if "ESTIMATED ARRIVAL" in status_upper:
                            match_arr = re.search(r"estimated\s+arrival\s*[:\s•]*(\d{1,2}:\d{2}\s*(?:AM|PM)?)", raw_status, re.I)
                            if match_arr:
                                t_str = match_arr.group(1).strip()
                                try:
                                    t_obj = datetime.strptime(t_str, "%I:%M %p")
                                    return t_obj.strftime("%H:%M"), arr_dt
                                except Exception:
                                    return t_str, arr_dt
        except Exception:
            pass
    return "--", None

def is_flight_candidate_for_live_check(f, now_vn):
    tz_vn = pytz.timezone("Asia/Ho_Chi_Minh")
    f_date = f.get("date", "")
    sta_str = f.get("scheduled", "")
    eta_viags_str = f.get("estimated", "")
    f_type = (f.get("flight_type") or "Domestic").upper()

    candidates = []
    for t_str in [sta_str, eta_viags_str]:
        if t_str and t_str != "--:--":
            try:
                dt = tz_vn.localize(datetime.strptime(f"{f_date} {t_str}", "%Y-%m-%d %H:%M"))
                if sta_str and t_str == eta_viags_str and eta_viags_str < sta_str and sta_str >= "20:00" and eta_viags_str <= "05:00":
                    dt = dt + timedelta(days=1)
                candidates.append(dt)
            except Exception:
                pass

    if not candidates:
        return False, None

    latest_milestone = max(candidates)
    max_lookahead_hours = 4.5 if f_type == "DOMESTIC" else 16.0
    is_eligible = (now_vn - timedelta(hours=4)) <= latest_milestone <= (now_vn + timedelta(hours=max_lookahead_hours))
    return is_eligible, latest_milestone

def load_day_flights(d):
    ref = db.reference(f"daily_schedules/{d}")
    data = ref.get()
    if data and isinstance(data, dict):
        return data.get("flights", [])
    return []

# ĐỌC NHÁNH ĐỘC LẬP THEO TỪNG NGÀY RIÊNG BIỆT (TUYỆT ĐỐI KHÔNG DÙNG CHUNG)
def load_live_statuses(d):
    ref = db.reference(f"live_statuses/{d}")
    data = ref.get()
    if data and isinstance(data, dict):
        return data
    return {}

# CẬP NHẬT TỪNG MỤC: CÓ KHÓA KIỂM TRA THỜI GIAN THỰC
def update_section_flights_cleanly(d, section_flights, sec_direction, sec_flight_type, tz_vn, is_status_only=False):
    ref = db.reference(f"daily_schedules/{d}")
    existing_data = ref.get()
    existing_list = []
    if existing_data and isinstance(existing_data, dict):
        existing_list = existing_data.get("flights", [])

    live_status_map = load_live_statuses(d)
    now_vn = datetime.now(tz_vn)

    if is_status_only:
        new_status_map = {f.get("flight_no"): f.get("remarks") for f in section_flights if f.get("flight_no")}
        updated_count = 0
        for item in existing_list:
            if item.get("direction") == sec_direction and item.get("flight_type") == sec_flight_type:
                f_no = item.get("flight_no")
                old_rem = (item.get("remarks") or "").strip().upper()
                if sec_direction == "Departure" and "DEPARTED" in old_rem:
                    continue
                if sec_direction == "Arrival" and "LANDED" in old_rem:
                    continue
                if f_no in new_status_map:
                    item["remarks"] = new_status_map[f_no]
                    updated_count += 1

        ref.set({
            "date": d,
            "flights": existing_list,
            "updated_at": datetime.now(tz_vn).strftime("%Y-%m-%d %H:%M:%S")
        })
        print(f"   💾 [STATUS CỘNG DỒN] Ngày {d} ({sec_direction} - {sec_flight_type}): Đã cập nhật trạng thái {updated_count} chuyến.")
        return

    # LÀM MỚI DANH SÁCH MỤC NÀY
    preserved_list = [
        f for f in existing_list 
        if not (f.get("direction") == sec_direction and f.get("flight_type") == sec_flight_type)
    ]

    cleaned_new_flights = []
    for f in section_flights:
        f_no = (f.get("flight_no") or "").replace(" ", "").upper()
        sched_str = f.get("scheduled", "--:--")

        is_future_date = (d > now_vn.strftime("%Y-%m-%d"))
        
        prior_status = live_status_map.get(f_no)
        if prior_status and not is_future_date:
            try:
                flight_sched_dt = tz_vn.localize(datetime.strptime(f"{d} {sched_str}", "%Y-%m-%d %H:%M"))
                if flight_sched_dt <= now_vn:
                    if sec_direction == "Departure" and "DEPARTED" in prior_status.upper():
                        f["remarks"] = prior_status
                    elif sec_direction == "Arrival" and "LANDED" in prior_status.upper():
                        f["remarks"] = prior_status
            except Exception:
                pass

        if is_future_date:
            curr_rem = (f.get("remarks") or "").strip().upper()
            if "DEPARTED" in curr_rem or "LANDED" in curr_rem:
                f["remarks"] = "Check-in open" if sec_direction == "Departure" else "Normal"

        cleaned_new_flights.append(f)

    final_list = preserved_list + cleaned_new_flights
    ref.set({
        "date": d,
        "flights": final_list,
        "updated_at": datetime.now(tz_vn).strftime("%Y-%m-%d %H:%M:%S")
    })
    print(f"   💾 [LƯU ĐẦY ĐỦ] Ngày {d} ({sec_direction} - {sec_flight_type}): Đã làm mới {len(cleaned_new_flights)} chuyến, tổng cộng kho có {len(final_list)} chuyến.")

def cleanup_old_data(now_vn):
    cutoff_date = (now_vn - timedelta(days=7)).strftime("%Y-%m-%d")
    for branch in ["daily_schedules", "fr24_etas", "live_statuses"]:
        try:
            ref = db.reference(branch)
            all_keys = ref.get(shallow=True)
            if isinstance(all_keys, dict):
                for date_key in list(all_keys.keys()):
                    if date_key < cutoff_date:
                        db.reference(f"{branch}/{date_key}").delete()
                        print(f"🧹 [DỌN DẸP TỰ ĐỘNG]: Đã xóa dữ liệu cũ {branch}/{date_key}")
        except Exception:
            pass

# HÀM QUÉT ĐỘC LẬP 3 TẦNG FLIGHTRADAR24
def run_fr24_cumulative_update(target_dates, now_vn):
    print(f"\n=======================================================")
    print(f" >>> QUÉT 3 TẦNG MỌI CHUYẾN BAY ĐANG LIVE TRÊN FR24 <<<")
    print(f" Thời điểm quét (VN): {now_vn.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"=======================================================")

    airborne_map = get_airborne_flights_to_sgn()
    print(f"✓ [TẦNG 1 - BẦU TRỜI]: Phát hiện {len(airborne_map)} máy bay đang bay: {list(airborne_map.keys())}")

    fast_live_cache = {}
    for f_no, f_id in airborne_map.items():
        eta_val, arr_dt = fetch_airborne_eta_by_id(f_id, f_no)
        if eta_val and eta_val != "--":
            fast_live_cache[f_no] = (eta_val, arr_dt)
            print(f"   ⚡ [TẦNG 1 LIVE] {f_no} -> ETA: {eta_val}")
        time.sleep(0.15)

    for d in target_dates:
        flights_list = load_day_flights(d)
        if not flights_list:
            continue

        fr24_ref = db.reference(f"fr24_etas/{d}")
        updated_count = 0

        for f in flights_list:
            if f.get("direction") == "Arrival":
                f_no = f.get("flight_no", "").replace(" ", "").upper()
                sta_str = f.get("scheduled", "--:--")
                rem_upper = (f.get("remarks") or "").upper()

                if "LANDED" in rem_upper or "ARRIVED" in rem_upper:
                    continue

                is_eligible, f_milestone_dt = is_flight_candidate_for_live_check(f, now_vn)
                if not is_eligible:
                    continue

                eta_val = "--"
                live_arr_dt = None

                if f_no in fast_live_cache:
                    eta_val, live_arr_dt = fast_live_cache[f_no]

                if eta_val == "--":
                    live_f_id = find_live_flight_id_via_search(f_no)
                    if live_f_id:
                        eta_val, live_arr_dt = fetch_airborne_eta_by_id(live_f_id, f_no)
                    time.sleep(0.15)

                if eta_val == "--":
                    eta_val, live_arr_dt = fetch_live_eta_via_list_api(f_no)
                    time.sleep(0.15)

                if eta_val and eta_val != "--" and is_valid_arrival_eta(eta_val, sta_str):
                    if live_arr_dt and f_milestone_dt:
                        if abs((live_arr_dt - f_milestone_dt).total_seconds()) > 10 * 3600:
                            continue

                    fr24_ref.update({f_no: eta_val})
                    updated_count += 1
                    print(f"   ✈️ [LƯU CỘNG DỒN FR24 - NGÀY {d}] {f_no} ({f.get('route', '')} | STA {sta_str}) => ETA: {eta_val}")

        print(f"✓ Ngày {d}: Đã lưu cộng dồn {updated_count} chuyến đang LIVE vào ngăn fr24_etas/{d}!")

def select_and_wait(page, selector, value):
    page.wait_for_selector(selector, state="attached", timeout=25000)
    try:
        with page.expect_response(lambda r: "flight-schedules" in r.url and r.request.method == "POST", timeout=20000):
            page.select_option(selector, value=value)
        time.sleep(2.5)
    except Exception:
        time.sleep(2.5)

# 2. Quy trình chính
def scrape_viags():
    init_firebase()
    tz_vn = pytz.timezone("Asia/Ho_Chi_Minh")
    now_vn = datetime.now(tz_vn)

    today_str = now_vn.strftime("%Y-%m-%d")
    tomorrow_str = (now_vn + timedelta(days=1)).strftime("%Y-%m-%d")
    day_after_str = (now_vn + timedelta(days=2)).strftime("%Y-%m-%d")
    target_dates = [today_str, tomorrow_str, day_after_str]

    scrape_mode = os.environ.get("SCRAPE_MODE", "scheduled_auto")

    # DỌN SẠCH CÁC TRẠNG THÁI BỊ RÒ RỈ CỦA NGÀY MAI VÀ NGÀY MỐT
    try:
        db.reference(f"live_statuses/{tomorrow_str}").delete()
        db.reference(f"live_statuses/{day_after_str}").delete()
    except Exception:
        pass

    # CHẾ ĐỘ 1: CẬP NHẬT RIÊNG FLIGHTRADAR24
    if scrape_mode == "fr24_only":
        run_fr24_cumulative_update(target_dates, now_vn)
        print("=== HOÀN TẤT QUÉT 3 TẦNG FLIGHTRADAR24 ===")
        return

    # CHẾ ĐỘ 2: CẬP NHẬT RIÊNG STATUS
    is_status_only = (scrape_mode == "status_only")
    if is_status_only:
        print(f"=======================================================")
        print(f" >>> [CHẾ ĐỘ STATUS SIÊU TỐC 30S]: NGÀY {today_str} <<<")
        print(f"=======================================================")
        scrape_dates = [today_str]
        max_load_clicks = 6
    elif scrape_mode == "today_only":
        if not load_day_flights(day_after_str):
            scrape_dates = [today_str, tomorrow_str, day_after_str]
        else:
            scrape_dates = [today_str]
        max_load_clicks = 25
        print(f">>> [PHẦN MỀM CHÍNH]: Quét ngày hôm nay {today_str} <<<")
    else:
        scrape_dates = [today_str, tomorrow_str, day_after_str]
        max_load_clicks = 25
        print(f">>> [PHẦN MỀM CHÍNH VIAGS]: Quét ĐẦY ĐỦ 3 ngày ({scrape_dates}) <<<")
        if scrape_mode == "scheduled_auto":
            run_fr24_cumulative_update(target_dates, now_vn)
            cleanup_old_data(now_vn)

    SEL_ORIGIN = "#ctl00_mainContent_ctl00_ddlOriginal"
    SEL_STATION = "#ctl00_mainContent_ctl00_ddlStation"
    SEL_TRIP = "#ctl00_mainContent_ctl00_ddlTrip"
    SEL_DATE = "#ctl00_mainContent_ctl00_datetimepicker1"

    sections = [
        {"name": "QUỐC NỘI - BAY ĐI", "station": "DOM", "trip": "D", "f_type": "Domestic", "d_type": "Departure"},
        {"name": "QUỐC NỘI - BAY ĐẾN", "station": "DOM", "trip": "A", "f_type": "Domestic", "d_type": "Arrival"},
        {"name": "QUỐC TẾ - BAY ĐI", "station": "INT", "trip": "D", "f_type": "International", "d_type": "Departure"},
        {"name": "QUỐC TẾ - BAY ĐẾN", "station": "INT", "trip": "A", "f_type": "International", "d_type": "Arrival"}
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900}
        )

        for current_date in scrape_dates:
            print(f"\n#######################################################")
            print(f" ####### QUÉT VIAGS NGÀY: {current_date} #######")
            print(f"#######################################################")

            live_status_ref = db.reference(f"live_statuses/{current_date}")
            if current_date == today_str:
                current_live_statuses = live_status_ref.get()
                if not isinstance(current_live_statuses, dict):
                    current_live_statuses = {}
            else:
                current_live_statuses = {}

            for sec in sections:
                print(f"\n--- [NGÀY {current_date}] ĐANG MỞ: {sec['name']} ---")

                for attempt in range(2):
                    page = context.new_page()
                    try:
                        url = "https://www.viags.vn/en-US/flight-schedules"
                        page.goto(url, wait_until="domcontentloaded", timeout=45000)
                        page.wait_for_selector(SEL_ORIGIN, state="attached", timeout=25000)
                        time.sleep(1.5)

                        page.evaluate(f"""(targetDate) => {{
                            const dateInp = document.querySelector('{SEL_DATE}, [name$="datetimepicker1"]');
                            if (dateInp && dateInp.value !== targetDate) {{
                                dateInp.value = targetDate;
                            }}
                        }}""", current_date)
                        time.sleep(0.3)

                        select_and_wait(page, SEL_ORIGIN, "SGN")

                        if sec["station"] != "DOM":
                            select_and_wait(page, SEL_STATION, sec["station"])

                        if sec["trip"] != "D":
                            select_and_wait(page, SEL_TRIP, sec["trip"])

                        for check_i in range(3):
                            headers_check = [th.inner_text().strip().lower() for th in page.locator(".c-table1 table thead tr th, table thead tr th").all()]
                            if sec["d_type"] == "Arrival" and ("destination" in headers_check) and ("from" not in headers_check):
                                select_and_wait(page, SEL_TRIP, "A")
                            elif sec["d_type"] == "Departure" and ("from" in headers_check) and ("destination" not in headers_check):
                                select_and_wait(page, SEL_TRIP, "D")
                            else:
                                break

                        load_count = 0
                        while True:
                            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                            time.sleep(0.8)

                            btn = page.locator(".c-btn1:not(.hidden):visible, span.c-btn1:not(.hidden):visible")
                            if btn.count() > 0 and btn.first.is_visible():
                                load_count += 1
                                try:
                                    btn.first.scroll_into_view_if_needed()
                                    btn.first.click(timeout=2500)
                                    time.sleep(1.8)
                                except Exception:
                                    break
                                if load_count >= max_load_clicks:
                                    break
                            else:
                                break

                        headers = [th.inner_text().strip().lower() for th in page.locator(".c-table1 table thead tr th, table thead tr th").all()]
                        rows = page.locator(".c-table1 table tbody tr, table tbody tr").all()

                        section_flights = []
                        status_updates_for_live = {}

                        for row in rows:
                            tds = row.locator("td").all()
                            if not tds or len(tds) < 5:
                                continue

                            col_map = {}
                            for idx, td in enumerate(tds):
                                h_name = headers[idx] if idx < len(headers) else f"col_{idx}"
                                col_map[h_name] = td.inner_text().strip()

                            row_raw_text = " ".join(col_map.values()).upper()
                            flight_no = col_map.get("flights", "").replace(" ", "").upper()
                            airline = col_map.get("airlines", "")

                            is_vna = (
                                flight_no.startswith("VN") or 
                                "VIETNAM AIRLINES" in airline.upper() or 
                                "VIETNAM AIRLINES" in row_raw_text
                            )

                            if not is_vna:
                                match_vn = re.search(r'\b(VN\s*\d{2,4})\b', row_raw_text)
                                if match_vn:
                                    is_vna = True
                                    flight_no = match_vn.group(1).replace(" ", "")

                            if is_vna and flight_no:
                                route = col_map.get("destination", "") if sec["d_type"] == "Departure" else col_map.get("from", "")
                                gate = col_map.get("gate", "--") if sec["d_type"] == "Departure" else "--"
                                if not gate:
                                    gate = "--"

                                belt = col_map.get("baggage belts", col_map.get("belt", "--")) if sec["d_type"] == "Arrival" else "--"
                                if not belt:
                                    belt = "--"

                                remarks = col_map.get("remarks", "Normal").strip()
                                if not remarks:
                                    remarks = "Normal"

                                direction_upper = sec["d_type"].upper()
                                sched_str = col_map.get("scheduled", "--:--")

                                if current_date == today_str:
                                    existing_status = current_live_statuses.get(flight_no)
                                    if existing_status:
                                        try:
                                            flight_sched_dt = tz_vn.localize(datetime.strptime(f"{current_date} {sched_str}", "%Y-%m-%d %H:%M"))
                                            if flight_sched_dt <= now_vn:
                                                if direction_upper == "DEPARTURE" and "DEPARTED" in existing_status.upper():
                                                    remarks = existing_status
                                                elif direction_upper == "ARRIVAL" and "LANDED" in existing_status.upper():
                                                    remarks = existing_status
                                        except Exception:
                                            pass

                                    if is_status_only:
                                        if remarks != "Normal" or flight_no not in current_live_statuses:
                                            status_updates_for_live[flight_no] = remarks
                                            current_live_statuses[flight_no] = remarks
                                else:
                                    rem_up = remarks.upper()
                                    if "DEPARTED" in rem_up or "LANDED" in rem_up:
                                        remarks = "Check-in open" if direction_upper == "DEPARTURE" else "Normal"

                                flight_item = {
                                    "date": current_date,
                                    "scheduled": col_map.get("scheduled", "--:--"),
                                    "estimated": col_map.get("estimated", col_map.get("scheduled", "--:--")),
                                    "destination_or_from": route,
                                    "route": route,
                                    "airline": "Vietnam Airlines",
                                    "flight_no": flight_no,
                                    "gate": gate,
                                    "belt": belt,
                                    "remarks": remarks,
                                    "flight_type": sec["f_type"],
                                    "direction": sec["d_type"],
                                    "airport": "SGN"
                                }
                                section_flights.append(flight_item)

                        update_section_flights_cleanly(current_date, section_flights, sec["d_type"], sec["f_type"], tz_vn, is_status_only=is_status_only)

                        if is_status_only and status_updates_for_live and current_date == today_str:
                            live_status_ref.update(status_updates_for_live)

                        print(f"✓ [NGÀY {current_date}] {sec['name']}: Đã tải {len(section_flights)} chuyến.")
                        page.close()
                        break
                    except Exception as ex:
                        try:
                            page.close()
                        except Exception:
                            pass
                        time.sleep(2)

        browser.close()

    print("=== ĐÃ HOÀN TẤT CẬP NHẬT CÁCH LY THEO NGÀY CHUẨN XÁC 100%! ===")

if __name__ == "__main__":
    scrape_viags()
