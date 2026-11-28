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

# TẦNG 1A: Quét radar bầu trời (Bao gồm cả máy bay đang bay qua biển/vệ tinh: estimated=1 & satellite=1)
def get_airborne_flights_to_sgn():
    airborne_map = {}  # { "VN409": "fr24_flight_id", ... }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Origin": "https://www.flightradar24.com",
        "Referer": "https://www.flightradar24.com/"
    }

    # Bật đầy đủ cờ vệ tinh (satellite=1), ước tính qua biển (estimated=1), adsb, mlat để không sót chuyến nào
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

                        # Bắt buộc điểm đến là SGN và máy bay ĐANG BAY TRÊN TRỜI
                        if dest != "SGN" or (on_ground == 1 and altitude <= 0):
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
        except Exception as e:
            print(f"Lưu ý khi quét tầng 1A ({url}): {e}")

    return airborne_map

# TẦNG 1B: Kiểm tra trực tiếp qua cổng Ô Tìm Kiếm LIVE FLIGHTS của Flightradar24 (Bắt 100% các chuyến như VN409, VN307, VN423)
def find_live_flight_id_via_search(flight_no):
    clean_no = flight_no.replace(" ", "").upper()
    callsign_no = clean_no.replace("VN", "HVN", 1) if clean_no.startswith("VN") else clean_no
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Origin": "https://www.flightradar24.com",
        "Referer": "https://www.flightradar24.com/"
    }

    try:
        url = f"https://www.flightradar24.com/v1/search/web/find?query={clean_no}&limit=15"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                for item in data.get("results", []):
                    # Chỉ lấy mục nằm trong khung LIVE FLIGHTS (type == 'live')
                    if item.get("type") == "live":
                        detail = item.get("detail", {})
                        schd_to = (detail.get("schd_to") or "").upper().strip()
                        f_code = (detail.get("flight") or "").upper().replace(" ", "").strip()
                        cs_code = (detail.get("callsign") or "").upper().replace(" ", "").strip()
                        label = (item.get("label") or "").upper().replace(" ", "").strip()

                        if schd_to == "SGN" and (f_code == clean_no or cs_code == callsign_no or clean_no in label):
                            return item.get("id")
    except Exception:
        pass
    return None

# TẦNG 2: Lấy chính xác GIỜ DỰ ĐÁP (ETA) và ngày đáp cho máy bay đang LIVE qua flight_id
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
                status_obj = data.get("status", {})
                raw_status_text = (status_obj.get("text") or "").strip()
                status_upper = raw_status_text.upper()

                time_info = data.get("time", {})
                real_arr = time_info.get("real", {}).get("arrival")
                est_arr = time_info.get("estimated", {}).get("arrival")
                sched_arr = time_info.get("scheduled", {}).get("arrival")

                # Tuyệt đối không lấy nếu đã hạ cánh
                if real_arr is not None or "LANDED" in status_upper or "ARRIVED" in status_upper:
                    return "--", None

                arr_dt = None
                if sched_arr:
                    arr_dt = datetime.fromtimestamp(sched_arr, tz_vn)
                elif est_arr:
                    arr_dt = datetime.fromtimestamp(est_arr, tz_vn)

                # Ưu tiên 1: Lấy từ trường timestamp dự đáp chính thức (time.estimated.arrival)
                if est_arr:
                    dt = datetime.fromtimestamp(est_arr, tz_vn)
                    return dt.strftime("%H:%M"), arr_dt

                # Ưu tiên 2: Lấy từ dòng chữ "Estimated arrival 2:20 PM"
                if "ESTIMATED ARRIVAL" in status_upper:
                    match_arr = re.search(r"estimated\s+arrival\s*[:\s•]*(\d{1,2}:\d{2}\s*(?:AM|PM)?)", raw_status_text, re.I)
                    if match_arr:
                        t_str = match_arr.group(1).strip()
                        try:
                            t_obj = datetime.strptime(t_str, "%I:%M %p")
                            return t_obj.strftime("%H:%M"), arr_dt
                        except Exception:
                            return t_str, arr_dt
    except Exception as e:
        print(f"Lưu ý clickhandler {flight_no}: {e}")

    return "--", None

# Kiểm tra chuyến bay đủ điều kiện quét: Chưa đáp (cho phép trễ qua giờ STA tối đa 4 tiếng như chuyến VN423) và không quá 20 tiếng tới
def is_flight_candidate_for_live_check(f, now_vn):
    tz_vn = pytz.timezone("Asia/Ho_Chi_Minh")
    f_date = f.get("date", "")
    sta_str = f.get("scheduled", "")
    eta_viags_str = f.get("estimated", "")

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
    # Cho phép quét cả những chuyến đang bị delay qua giờ STA tới 4 tiếng (như VN423 STA 12:35 nhưng 13:32 mới đáp)
    is_eligible = (now_vn - timedelta(hours=4)) <= latest_milestone <= (now_vn + timedelta(hours=24))
    return is_eligible, latest_milestone

# Đọc danh sách chuyến bay VIAGS từ ngăn chứa 1 (daily_schedules)
def load_day_flights(d):
    ref = db.reference(f"daily_schedules/{d}")
    data = ref.get()
    if data and isinstance(data, dict):
        return data.get("flights", [])
    return []

# Lưu danh sách chuyến bay VIAGS vào ngăn chứa 1 (daily_schedules)
def save_viags_day_flights(d, flights_list, tz_vn):
    ref = db.reference(f"daily_schedules/{d}")
    ref.set({
        "date": d,
        "flights": flights_list,
        "updated_at": datetime.now(tz_vn).strftime("%Y-%m-%d %H:%M:%S")
    })

def select_and_wait(page, selector, value):
    page.wait_for_selector(selector, state="attached", timeout=25000)
    try:
        with page.expect_response(lambda r: "flight-schedules" in r.url and r.request.method == "POST", timeout=20000):
            page.select_option(selector, value=value)
        time.sleep(3)
    except Exception:
        time.sleep(3)

# 2. Quy trình chính
def scrape_viags():
    init_firebase()
    tz_vn = pytz.timezone("Asia/Ho_Chi_Minh")
    now_vn = datetime.now(tz_vn)

    today_str = now_vn.strftime("%Y-%m-%d")
    tomorrow_str = (now_vn + timedelta(days=1)).strftime("%Y-%m-%d")
    target_dates = [today_str, tomorrow_str]

    scrape_mode = os.environ.get("SCRAPE_MODE", "both_days")

    # =========================================================================
    # CHỨC NĂNG ĐỘC LẬP: QUÉT TOÀN BỘ MÁY BAY ĐANG LIVE & CẬP NHẬT fr24_etas
    # =========================================================================
    if scrape_mode == "fr24_only":
        print(f"=======================================================")
        print(f" >>> QUÉT TOÀN DIỆN MỌI CHUYẾN BAY ĐANG LIVE TRÊN FR24 <<<")
        print(f" Thời điểm quét (VN): {now_vn.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"=======================================================")

        # Bước 1: Quét bản đồ bầu trời (bao gồm cả vệ tinh & biển khơi)
        airborne_map = get_airborne_flights_to_sgn()
        print(f"✓ [BƯỚC 1 - BẦU TRỜI]: Phát hiện {len(airborne_map)} chuyến trên feed: {list(airborne_map.keys())}")

        # Bước 2: Đối chiếu với danh sách chuyến bay chưa đáp trong bảng và kiểm tra bổ sung qua cổng Search LIVE FLIGHTS
        for d in target_dates:
            flights_list = load_day_flights(d)
            if not flights_list:
                print(f"Ngày {d} chưa có danh sách VIAGS trong daily_schedules.")
                continue

            fr24_ref = db.reference(f"fr24_etas/{d}")
            updates_for_day = {}

            for f in flights_list:
                if f.get("direction") == "Arrival":
                    f_no = f.get("flight_no", "").replace(" ", "").upper()
                    sta_str = f.get("scheduled", "--:--")
                    rem_upper = (f.get("remarks") or "").upper()

                    # Bỏ qua nếu chuyến bay đã có trạng thái Landed (Đã hạ cánh)
                    if "LANDED" in rem_upper or "ARRIVED" in rem_upper:
                        continue

                    is_eligible, f_milestone_dt = is_flight_candidate_for_live_check(f, now_vn)
                    if not is_eligible:
                        continue

                    # Tìm flight_id đang LIVE (từ bản đồ bầu trời hoặc từ ô tìm kiếm LIVE FLIGHTS)
                    live_f_id = airborne_map.get(f_no)
                    if not live_f_id:
                        live_f_id = find_live_flight_id_via_search(f_no)
                        time.sleep(0.15)

                    if live_f_id:
                        eta_val, live_arr_dt = fetch_airborne_eta_by_id(live_f_id, f_no)
                        time.sleep(0.2)

                        if eta_val and eta_val != "--" and is_valid_arrival_eta(eta_val, sta_str):
                            # Đảm bảo chuyến đang LIVE thuộc đúng ngày d (không gán nhầm chuyến đang bay hôm nay vào lịch của chiều mai)
                            if live_arr_dt and f_milestone_dt:
                                if abs((live_arr_dt - f_milestone_dt).total_seconds()) > 10 * 3600:
                                    continue

                            updates_for_day[f_no] = eta_val
                            print(f"   ✈️ [LIVE TRÊN FR24 - NGÀY {d}] {f_no} ({f.get('route', '')} | STA {sta_str}) => ETA DỰ ĐÁP: {eta_val}")

            # DÙNG LỆNH .update() ĐỂ GIỮ NGUYÊN VĨNH VIỄN CÁC CHUYẾN ĐÃ CÓ DỮ LIỆU TỪ TRƯỚC, KHÔNG BAO GIỜ XÓA VỀ RADAR LIVE!
            if updates_for_day:
                fr24_ref.update(updates_for_day)
                print(f"✓ Ngày {d}: Đã cập nhật cộng dồn {len(updates_for_day)} chuyến đang LIVE vào ngăn fr24_etas/{d}!")
            else:
                print(f"✓ Ngày {d}: Giữ nguyên 100% các ETA đã hiển thị trước đó!")

        print("=== HOÀN TẤT QUÉT TOÀN DIỆN FLIGHTRADAR24 ===")
        return

    # =========================================================================
    # PHẦN MỀM CHÍNH: CHỈ CẬP NHẬT NGĂN CHỨA 1 (daily_schedules)
    # TUYỆT ĐỐI KHÔNG ĐỤNG CHẠM TỚI NGĂN fr24_etas!
    # =========================================================================
    if scrape_mode == "today_only":
        if not load_day_flights(tomorrow_str):
            scrape_dates = [today_str, tomorrow_str]
            print(f">>> [PHẦN MỀM CHÍNH]: Cào cả {today_str} và {tomorrow_str} vào daily_schedules <<<")
        else:
            scrape_dates = [today_str]
            print(f">>> [PHẦN MỀM CHÍNH]: Chỉ cập nhật VIAGS ngày {today_str} vào daily_schedules <<<")
    else:
        scrape_dates = [today_str, tomorrow_str]
        print(f">>> [PHẦN MỀM CHÍNH]: Cập nhật VIAGS 2 ngày ({scrape_dates}) vào daily_schedules <<<")

    all_flights_by_date = {d: [] for d in scrape_dates}

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
            print(f" ####### BẮT ĐẦU QUÉT VIAGS NGÀY: {current_date} #######")
            print(f"#######################################################")

            for sec in sections:
                print(f"\n--- [NGÀY {current_date}] ĐANG MỞ TRANG: {sec['name']} ---")

                for attempt in range(3):
                    page = context.new_page()
                    try:
                        url = "https://www.viags.vn/en-US/flight-schedules"
                        page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_selector(SEL_ORIGIN, state="attached", timeout=30000)
                        time.sleep(2)

                        page.evaluate(f"""(targetDate) => {{
                            const dateInp = document.querySelector('{SEL_DATE}, [name$="datetimepicker1"]');
                            if (dateInp && dateInp.value !== targetDate) {{
                                dateInp.value = targetDate;
                            }}
                        }}""", current_date)
                        time.sleep(0.5)

                        print(f"1. Chọn Sân bay: Ho Chi Minh (SGN) cho ngày {current_date}...")
                        select_and_wait(page, SEL_ORIGIN, "SGN")

                        if sec["station"] != "DOM":
                            print(f"2. Chọn Loại chuyến: {sec['station']}...")
                            select_and_wait(page, SEL_STATION, sec["station"])

                        if sec["trip"] != "D":
                            print(f"3. Chọn Chiều bay: {sec['trip']}...")
                            select_and_wait(page, SEL_TRIP, sec["trip"])

                        load_count = 0
                        while True:
                            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                            time.sleep(1)

                            btn = page.locator(".c-btn1:not(.hidden):visible, span.c-btn1:not(.hidden):visible")
                            if btn.count() > 0 and btn.first.is_visible():
                                load_count += 1
                                try:
                                    btn.first.scroll_into_view_if_needed()
                                    btn.first.click(timeout=3000)
                                    time.sleep(2.2)
                                except Exception:
                                    break
                                if load_count >= 25:
                                    break
                            else:
                                break

                        headers = [th.inner_text().strip().lower() for th in page.locator(".c-table1 table thead tr th, table thead tr th").all()]
                        rows = page.locator(".c-table1 table tbody tr, table tbody tr").all()
                        print(f"Tổng số dòng trong bảng: {len(rows)} (Đã bấm {load_count} lần Load more)")

                        section_vna = 0
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

                                remarks = col_map.get("remarks", "Normal")
                                if not remarks:
                                    remarks = "Normal"

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
                                all_flights_by_date[current_date].append(flight_item)
                                section_vna += 1

                        print(f"✓ [NGÀY {current_date}] LẤY ĐƯỢC: {section_vna} chuyến bay {sec['name']}.")
                        page.close()
                        break
                    except Exception as ex:
                        print(f"   [Cảnh báo] Lần thử {attempt + 1} gặp lỗi mạng VIAGS: {ex}")
                        try:
                            page.close()
                        except Exception:
                            pass
                        time.sleep(3)

        browser.close()

    # LƯU RIÊNG VÀO NGĂN daily_schedules (KHÔNG BAO GIỜ ĐỤNG TỚI NGĂN fr24_etas)
    print(f"\n=======================================================")
    print(f" LƯU VIAGS VÀO NGĂN daily_schedules (ĐỘC LẬP 100% VỚI FR24)")
    print(f"=======================================================")
    for d, flights_list in all_flights_by_date.items():
        if len(flights_list) > 0:
            save_viags_day_flights(d, flights_list, tz_vn)
            print(f"✓ Đã lưu thành công {len(flights_list)} chuyến bay ngày {d} vào daily_schedules!")

    print("=== ĐÃ HOÀN TẤT CẬP NHẬT PHẦN MỀM CHÍNH! ===")

if __name__ == "__main__":
    scrape_viags()
