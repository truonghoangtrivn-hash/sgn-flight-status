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

# Kiểm tra chéo: Giờ dự đáp (ETA) không thể sớm hơn giờ kế hoạch (STA) quá 45 phút (Chặn tuyệt đối giờ cất cánh ETD)
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

# QUY TẮC 3 & 4: Chỉ lấy khi chuyến bay ĐANG BAY và chỉ lấy GIỜ DỰ ĐÁP (ETA)
def fetch_flight_eta_from_fr24(flight_no, flight_date_str, scheduled_time_str):
    clean_no = flight_no.replace(" ", "").upper()
    tz_vn = pytz.timezone('Asia/Ho_Chi_Minh')
    now_vn = datetime.now(tz_vn)

    queries = [clean_no]
    if clean_no.startswith("VN"):
        queries.append(clean_no.replace("VN", "HVN"))

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Origin": "https://www.flightradar24.com",
        "Referer": "https://www.flightradar24.com/"
    }

    for q in queries:
        try:
            url = f"https://api.flightradar24.com/common/v1/flight/list.json?query={q}&fetchBy=flight&page=1&limit=8"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode('utf-8'))
                    items = data.get("result", {}).get("response", {}).get("data", [])

                    for item in items:
                        # 1. Điểm đến bắt buộc phải là SGN
                        dest = (item.get("airport", {}).get("destination", {}).get("code", {}).get("iata") or "").upper()
                        if dest != "SGN":
                            continue

                        status_obj = item.get("status", {})
                        is_live = bool(status_obj.get("live", False))
                        raw_status_text = (status_obj.get("text") or "").strip()
                        status_upper = raw_status_text.upper()

                        time_info = item.get("time", {})
                        real_dep = time_info.get("real", {}).get("departure")
                        real_arr = time_info.get("real", {}).get("arrival")
                        est_arr = time_info.get("estimated", {}).get("arrival")

                        # 2. QUY TẮC 3: Tuyệt đối không lấy chuyến đã đáp hoặc bị hủy
                        if real_arr is not None or any(w in status_upper for w in ["LANDED", "ARRIVED", "CANCELED", "CANCELLED", "DIVERTED"]):
                            continue

                        # 3. QUY TẮC 3: Máy bay BẮT BUỘC phải đang bay (LIVE hoặc đã cất cánh hoặc đang có Estimated arrival)
                        has_est_arrival_text = "ESTIMATED ARRIVAL" in status_upper
                        is_airborne = is_live or (real_dep is not None and real_arr is None) or has_est_arrival_text
                        if not is_airborne:
                            continue

                        # 4. QUY TẮC 4: CHỈ LẤY GIỜ DỰ ĐÁP (ETA), TUYỆT ĐỐI KHÔNG LẤY ETD
                        candidate_eta = None

                        # Ưu tiên A: Lấy từ dòng chữ ghi rõ "Estimated arrival HH:MM" trên FR24
                        if has_est_arrival_text:
                            match_arr = re.search(r'estimated\s+arrival\s*[:\s•]*(\d{1,2}:\d{2}\s*(?:AM|PM)?)', raw_status_text, re.I)
                            if match_arr:
                                t_str = match_arr.group(1).strip()
                                try:
                                    t_obj = datetime.strptime(t_str, "%I:%M %p")
                                    candidate_eta = t_obj.strftime("%H:%M")
                                except Exception:
                                    candidate_eta = t_str

                        # Ưu tiên B: Lấy từ trường timestamp dự đáp (time.estimated.arrival)
                        if not candidate_eta and est_arr:
                            est_dt = datetime.fromtimestamp(est_arr, tz_vn)
                            # Phải là chuyến bay có giờ dự đáp trong hiện tại/tương lai (không lấy chuyến cũ của ngày hôm qua)
                            if est_dt >= (now_vn - timedelta(minutes=30)):
                                candidate_eta = est_dt.strftime('%H:%M')

                        if candidate_eta and is_valid_arrival_eta(candidate_eta, scheduled_time_str):
                            return candidate_eta

            time.sleep(0.35)
        except Exception:
            pass

    return "--"

# QUY TẮC 1: Chỉ cập nhật cho các chuyến có Max(STA, ETA) SAU giờ hiện tại
def is_flight_after_now(f, now_vn):
    tz_vn = pytz.timezone('Asia/Ho_Chi_Minh')
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
        return False

    latest_milestone = max(candidates)
    return latest_milestone > now_vn

# Đọc danh sách chuyến bay VIAGS từ ngăn chứa 1 (daily_schedules)
def load_day_flights(d):
    ref = db.reference(f"daily_schedules/{d}")
    data = ref.get()
    if data and isinstance(data, dict):
        return data.get("flights", [])
    return []

# Lưu danh sách chuyến bay VIAGS vào ngăn chứa 1 (daily_schedules) - KHÔNG ĐỤNG TỚI NGĂN FR24
def save_viags_day_flights(d, flights_list, tz_vn):
    ref = db.reference(f"daily_schedules/{d}")
    ref.set({
        "date": d,
        "flights": flights_list,
        "updated_at": datetime.now(tz_vn).strftime('%Y-%m-%d %H:%M:%S')
    })

# Hàm đổi ô chọn an toàn trên VIAGS
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
    tz_vn = pytz.timezone('Asia/Ho_Chi_Minh')
    now_vn = datetime.now(tz_vn)

    today_str = now_vn.strftime('%Y-%m-%d')
    tomorrow_str = (now_vn + timedelta(days=1)).strftime('%Y-%m-%d')
    target_dates = [today_str, tomorrow_str]

    scrape_mode = os.environ.get("SCRAPE_MODE", "both_days")

    # =========================================================================
    # CHỨC NĂNG ĐỘC LẬP: CHỈ CẬP NHẬT NGĂN CHỨA 2 (fr24_etas)
    # =========================================================================
    if scrape_mode == "fr24_only":
        print(f"=======================================================")
        print(f" >>> CẬP NHẬT ĐỘC LẬP NGĂN FLIGHTRADAR24 (fr24_etas) <<<")
        print(f" Giờ hiện tại (VN): {now_vn.strftime('%Y-%m-%d %H:%M')}")
        print(f"=======================================================")

        for d in target_dates:
            flights_list = load_day_flights(d)
            if not flights_list:
                print(f"Ngày {d} chưa có danh sách VIAGS.")
                continue

            # Đọc dữ liệu FR24 hiện có trong ngăn độc lập fr24_etas/{d}
            fr24_ref = db.reference(f"fr24_etas/{d}")
            current_fr24_map = fr24_ref.get()
            if not isinstance(current_fr24_map, dict):
                current_fr24_map = {}

            updated_count = 0
            checked_count = 0

            for f in flights_list:
                if f.get("direction") == "Arrival":
                    f_no = f.get("flight_no", "").replace(" ", "").upper()
                    sta_str = f.get("scheduled", "--:--")
                    rem_upper = (f.get("remarks") or "").upper()

                    # Quy tắc 1: Chỉ quét các chuyến bay có Max(STA, ETA) sau giờ hiện tại
                    if "LANDED" not in rem_upper and is_flight_after_now(f, now_vn):
                        checked_count += 1
                        eta = fetch_flight_eta_from_fr24(f_no, f["date"], sta_str)
                        if eta and eta != "--":
                            current_fr24_map[f_no] = eta
                            updated_count += 1
                            print(f"   -> [ĐANG BAY - ETA DỰ ĐÁP] {f_no} ({f.get('route', '')} | STA {sta_str}): {eta}")
                        time.sleep(0.35)

            # Lưu trực tiếp vào ngăn độc lập fr24_etas/{d}
            if current_fr24_map:
                fr24_ref.set(current_fr24_map)
            print(f"✓ Ngày {d}: Đã kiểm tra {checked_count} chuyến chưa đáp, cập nhật {updated_count} chuyến đang bay vào ngăn fr24_etas/{d}!")

        print("=== HOÀN TẤT CẬP NHẬT ĐỘC LẬP FLIGHTRADAR24 ===")
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
