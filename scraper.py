import json
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta
import pytz
from playwright.sync_api import sync_playwright
import firebase_admin
from firebase_admin import credentials, firestore

# 1. Khởi tạo kết nối Firebase
def init_firebase():
    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not sa_json:
        raise ValueError("Thiếu biến môi trường FIREBASE_SERVICE_ACCOUNT!")
    cred_dict = json.loads(sa_json)
    cred = credentials.Certificate(cred_dict)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    return firestore.client()

# QUY TẮC 3 & 4: Chỉ lấy khi chuyến bay ĐANG BAY và chỉ lấy GIỜ DỰ ĐÁP (ETA), tuyệt đối không lấy ETD hoặc chuyến đã đáp
def fetch_flight_eta_from_fr24(flight_no, flight_date_str, scheduled_time_str):
    clean_no = flight_no.replace(" ", "").upper()
    tz_vn = pytz.timezone('Asia/Ho_Chi_Minh')

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
                        # Phải là chuyến bay có điểm đến là SGN
                        dest = (item.get("airport", {}).get("destination", {}).get("code", {}).get("iata") or "").upper()
                        if dest != "SGN":
                            continue

                        status_obj = item.get("status", {})
                        is_live = status_obj.get("live", False)
                        raw_status_text = (status_obj.get("text") or "").strip()
                        status_upper = raw_status_text.upper()

                        time_info = item.get("time", {})
                        real_dep = time_info.get("real", {}).get("departure")
                        real_arr = time_info.get("real", {}).get("arrival")
                        est_arr = time_info.get("estimated", {}).get("arrival")

                        # QUY TẮC 3: Tuyệt đối không lấy chuyến đã đáp (Landed / Arrived / có real_arr)
                        if real_arr is not None or "LANDED" in status_upper or "ARRIVED" in status_upper:
                            continue

                        # QUY TẮC 3: Bắt buộc chuyến bay phải ĐANG BAY (LIVE trên radar hoặc đã cất cánh thực tế)
                        # Nếu máy bay chưa cất cánh -> Bỏ qua, không lấy thông tin giả định!
                        is_airborne = is_live or (real_dep is not None)
                        if not is_airborne:
                            continue

                        # QUY TẮC 4: CHỈ LẤY GIỜ DỰ ĐÁP (ETA), TUYỆT ĐỐI KHÔNG LẤY GIỜ CẤT CÁNH (ETD)
                        # Cách 1: Lấy từ trường timestamp dự đáp chính thức (time.estimated.arrival)
                        if est_arr:
                            dt = datetime.fromtimestamp(est_arr, tz_vn)
                            return dt.strftime('%H:%M')

                        # Cách 2: Lấy từ chữ "Estimated arrival HH:MM" (Bắt buộc phải có chữ arrival)
                        match_arr = re.search(r'estimated\s+arrival\s*[:\s•]*(\d{1,2}:\d{2}\s*(?:AM|PM)?)', raw_status_text, re.I)
                        if match_arr:
                            t_str = match_arr.group(1).strip()
                            try:
                                t_obj = datetime.strptime(t_str, "%I:%M %p")
                                return t_obj.strftime("%H:%M")
                            except Exception:
                                return t_str

            time.sleep(0.4)
        except Exception:
            pass

    return "--"

# QUY TẮC 1: Chỉ cập nhật cho các chuyến có STA hoặc ETA (chọn mốc trễ hơn) SAU giờ hiện tại
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
                # Xử lý trường hợp chuyến bay khuya bị delay qua 00:00 sáng hôm sau
                if sta_str and t_str == eta_viags_str and eta_viags_str < sta_str and sta_str >= "20:00" and eta_viags_str <= "05:00":
                    dt = dt + timedelta(days=1)
                candidates.append(dt)
            except Exception:
                pass

    if not candidates:
        return False

    # Chọn mốc nào trễ hơn giữa STA và ETA
    latest_milestone = max(candidates)

    # Chỉ quét các chuyến có mốc trễ hơn giờ hiện tại (bao gồm cả ngày hôm sau D+1)
    return latest_milestone > now_vn

# Hàm đổi ô chọn và chờ phản hồi mạng
def select_and_wait(page, selector, value):
    try:
        with page.expect_response(lambda r: "flight-schedules" in r.url and r.request.method == "POST", timeout=20000):
            page.select_option(selector, value=value)
        time.sleep(3)
    except Exception:
        page.select_option(selector, value=value)
        time.sleep(4)

# 2. Quy trình chính
def scrape_viags():
    db = init_firebase()
    tz_vn = pytz.timezone('Asia/Ho_Chi_Minh')
    now_vn = datetime.now(tz_vn)

    today_str = now_vn.strftime('%Y-%m-%d')
    tomorrow_str = (now_vn + timedelta(days=1)).strftime('%Y-%m-%d')
    target_dates = [today_str, tomorrow_str]

    scrape_mode = os.environ.get("SCRAPE_MODE", "both_days")

    # =========================================================================
    # CHẾ ĐỘ ĐỘC LẬP: CHỈ CẬP NHẬT CỘT FLIGHTRADAR24 (KHI BẤM NÚT CẬP NHẬT FR24)
    # =========================================================================
    if scrape_mode == "fr24_only":
        print(f"=======================================================")
        print(f" >>> CẬP NHẬT ĐỘC LẬP CỘT FLIGHTRADAR24 THEO 4 QUY TẮC <<<")
        print(f" Giờ hiện tại: {now_vn.strftime('%Y-%m-%d %H:%M')}")
        print(f"=======================================================")

        for d in target_dates:
            try:
                doc_ref = db.collection("daily_schedules").document(d)
                doc_snap = doc_ref.get()
                if doc_snap.exists:
                    data = doc_snap.to_dict()
                    flights_list = data.get("flights", [])
                    updated = False

                    for f in flights_list:
                        if f.get("direction") == "Arrival":
                            # Quy tắc 1: Mốc Max(STA, ETA) phải sau giờ hiện tại
                            if is_flight_after_now(f, now_vn):
                                eta = fetch_flight_eta_from_fr24(f["flight_no"], f["date"], f["scheduled"])
                                old_val = f.get("fr24_eta", "--")
                                if eta != old_val:
                                    f["fr24_eta"] = eta
                                    updated = True
                                if eta != "--":
                                    print(f"   -> [ĐANG BAY - ETA DỰ ĐÁP] {f['flight_no']} ({f.get('route', '')}): {eta}")
                                time.sleep(0.4)

                    if updated:
                        doc_ref.set({"date": d, "flights": flights_list, "updated_at": firestore.SERVER_TIMESTAMP})
                        print(f"✓ Đã lưu cột Flightradar24 độc lập cho ngày {d}!")
            except Exception as e:
                print(f"Lỗi đọc Firebase ngày {d}: {e}")

        print("=== HOÀN TẤT CẬP NHẬT ĐỘC LẬP FLIGHTRADAR24 ===")
        return

    # =========================================================================
    # PHẦN MỀM CHÍNH: CHỈ CÀO VIAGS (TUYỆT ĐỐI KHÔNG GHI ĐÈ CỘT FLIGHTRADAR24)
    # =========================================================================
    if scrape_mode == "today_only":
        scrape_dates = [today_str]
        print(f">>> [PHẦN MỀM CHÍNH]: CHỈ CẬP NHẬT VIAGS NGÀY HÔM NAY ({today_str}) <<<")
    else:
        scrape_dates = [today_str, tomorrow_str]
        print(f">>> [PHẦN MỀM CHÍNH]: CẬP NHẬT VIAGS CẢ 2 NGÀY ({scrape_dates}) <<<")

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

                page = context.new_page()
                url = "https://www.viags.vn/en-US/flight-schedules"
                page.goto(url, wait_until="networkidle", timeout=60000)
                time.sleep(2)

                page.evaluate(f"""(targetDate) => {{
                    const dateInp = document.querySelector('{SEL_DATE}, [name$="datetimepicker1"]');
                    if (dateInp) {{
                        dateInp.value = targetDate;
                        if (window.jQuery) window.jQuery(dateInp).trigger('change');
                        dateInp.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    }}
                }}""", current_date)
                time.sleep(1)

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
                        print(f"   -> Bấm 'Load more' lần {load_count}...")
                        try:
                            btn.first.scroll_into_view_if_needed()
                            btn.first.click(timeout=3000)
                            time.sleep(2.5)
                        except Exception:
                            break

                        if load_count >= 25:
                            break
                    else:
                        print(f"   -> Đã tải xong toàn bộ (đã bấm {load_count} lần 'Load more').")
                        break

                headers = [th.inner_text().strip().lower() for th in page.locator(".c-table1 table thead tr th, table thead tr th").all()]
                rows = page.locator(".c-table1 table tbody tr, table tbody tr").all()
                print(f"Tổng số dòng trong bảng: {len(rows)}")

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
                        if not gate or gate == "":
                            gate = "--"

                        belt = col_map.get("baggage belts", col_map.get("belt", "--")) if sec["d_type"] == "Arrival" else "--"
                        if not belt or belt == "":
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
                            "airport": "SGN",
                            "fr24_url": f"https://www.flightradar24.com/data/flights/{flight_no}",
                            "fr24_eta": "--"
                        }
                        all_flights_by_date[current_date].append(flight_item)
                        section_vna += 1

                print(f"✓ [NGÀY {current_date}] LẤY ĐƯỢC: {section_vna} chuyến bay {sec['name']}.")
                page.close()

        browser.close()

    # QUY TẮC 2: LƯU VIAGS NHƯNG BẢO TOÀN 100% DỮ LIỆU ĐỘC LẬP CỦA CỘT FLIGHTRADAR24
    print(f"\n=======================================================")
    print(f" LƯU VIAGS & GIỮ NGUYÊN CỘT FLIGHTRADAR24 ĐỘC LẬP")
    print(f"=======================================================")
    for d, flights_list in all_flights_by_date.items():
        if len(flights_list) > 0:
            try:
                doc_ref = db.collection("daily_schedules").document(d)
                
                # Đọc lại dữ liệu FR24 đang có để bảo toàn tuyệt đối, không ghi đè
                existing_fr24_map = {}
                old_snap = doc_ref.get()
                if old_snap.exists:
                    for old_f in old_snap.to_dict().get("flights", []):
                        if old_f.get("direction") == "Arrival" and old_f.get("fr24_eta") and old_f.get("fr24_eta") != "--":
                            existing_fr24_map[old_f.get("flight_no")] = old_f.get("fr24_eta")

                # Gắn trả lại dữ liệu FR24 cũ vào danh sách mới của VIAGS
                for f in flights_list:
                    if f["direction"] == "Arrival" and f["flight_no"] in existing_fr24_map:
                        f["fr24_eta"] = existing_fr24_map[f["flight_no"]]

                doc_ref.set({
                    "date": d,
                    "flights": flights_list,
                    "updated_at": firestore.SERVER_TIMESTAMP
                })
                print(f"✓ Đã lưu thành công {len(flights_list)} chuyến bay ngày {d} (Bảo toàn cột FR24).")
            except Exception as e:
                print(f"Lưu ý Firebase ngày {d}: {e}")

    print("=== ĐÃ HOÀN TẤT CẬP NHẬT PHẦN MỀM CHÍNH! ===")

if __name__ == "__main__":
    scrape_viags()
