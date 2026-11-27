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

# Hàm tra cứu trực tiếp ETA từng chuyến bay trên Flightradar24 (Hỗ trợ cả VN và HVN)
def fetch_flight_eta_from_fr24(flight_no):
    clean_no = flight_no.replace(" ", "").upper()
    tz_vn = pytz.timezone('Asia/Ho_Chi_Minh')

    # Thử cả 2 định dạng: IATA (VN...) và ICAO (HVN...)
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
            url = f"https://api.flightradar24.com/common/v1/flight/list.json?query={q}&fetchBy=flight&page=1&limit=5"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode('utf-8'))
                    items = data.get("result", {}).get("response", {}).get("data", [])
                    
                    for item in items:
                        dest = (item.get("airport", {}).get("destination", {}).get("code", {}).get("iata") or "").upper()
                        # Đúng chuyến hạ cánh SGN
                        if dest == "SGN" or not dest:
                            time_info = item.get("time", {})
                            est_ts = time_info.get("estimated", {}).get("arrival")
                            real_ts = time_info.get("real", {}).get("arrival")
                            ts = est_ts or real_ts

                            # 1. Trích xuất từ timestamp
                            if ts:
                                dt = datetime.fromtimestamp(ts, tz_vn)
                                return dt.strftime('%H:%M')

                            # 2. Trích xuất từ status text (VD: Estimated arrival 12:31 AM hoặc Estimated 05:30)
                            status_text = (item.get("status", {}).get("text") or "").strip()
                            if status_text:
                                match_am_pm = re.search(r'(\d{1,2}:\d{2})\s*(AM|PM)', status_text, re.I)
                                if match_am_pm:
                                    t_str = match_am_pm.group(0).strip()
                                    try:
                                        t_obj = datetime.strptime(t_str, "%I:%M %p")
                                        return t_obj.strftime("%H:%M")
                                    except Exception:
                                        return t_str

                                match_24h = re.search(r'(\d{1,2}:\d{2})', status_text)
                                if match_24h:
                                    return match_24h.group(1)
            time.sleep(0.5)
        except Exception:
            pass

    return "--"

# Hàm đổi ô chọn và chờ phản hồi mạng
def select_and_wait(page, selector, value):
    try:
        with page.expect_response(lambda r: "flight-schedules" in r.url and r.request.method == "POST", timeout=20000):
            page.select_option(selector, value=value)
        time.sleep(3)
    except Exception:
        page.select_option(selector, value=value)
        time.sleep(4)

# 2. Quy trình cào dữ liệu SGN
def scrape_viags():
    db = init_firebase()
    tz_vn = pytz.timezone('Asia/Ho_Chi_Minh')
    now_vn = datetime.now(tz_vn)

    today_str = now_vn.strftime('%Y-%m-%d')
    tomorrow_str = (now_vn + timedelta(days=1)).strftime('%Y-%m-%d')
    target_dates = [today_str, tomorrow_str]
    window_limit = now_vn + timedelta(hours=16)
    check_start = now_vn - timedelta(hours=1)

    scrape_mode = os.environ.get("SCRAPE_MODE", "both_days")

    # =======================================================
    # CHẾ ĐỘ SIÊU TỐC: CẬP NHẬT RIÊNG FLIGHTRADAR24 ETA
    # =======================================================
    if scrape_mode == "fr24_only":
        print(f"=======================================================")
        print(f" >>> [CHẾ ĐỘ SIÊU TỐC]: CẬP NHẬT FLIGHTRADAR24 (16H TỚI) <<<")
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
                            try:
                                flight_dt = tz_vn.localize(datetime.strptime(f"{f['date']} {f['scheduled']}", "%Y-%m-%d %H:%M"))
                                if check_start <= flight_dt <= window_limit:
                                    eta = fetch_flight_eta_from_fr24(f["flight_no"])
                                    if eta and eta != "--":
                                        f["fr24_eta"] = eta
                                        updated = True
                                        print(f"   -> [CẬP NHẬT FR24 ETA] {f['flight_no']} ({f.get('route', '')}): {eta}")
                                    time.sleep(0.8) # Nghỉ nhẹ giữa các chuyến
                            except Exception:
                                pass

                    if updated:
                        doc_ref.set({"date": d, "flights": flights_list, "updated_at": firestore.SERVER_TIMESTAMP})
                        print(f"✓ Đã lưu cập nhật FR24 cho ngày {d}!")
            except Exception as e:
                print(f"Lỗi đọc Firebase ngày {d}: {e}")

        print("=== HOÀN TẤT CẬP NHẬT FR24 SIÊU TỐC ===")
        return

    # =======================================================
    # CHẾ ĐỘ TIÊU CHUẨN: CÀO VIAGS + QUÉT FLIGHTRADAR24
    # =======================================================
    if scrape_mode == "today_only":
        scrape_dates = [today_str]
        print(f">>> [CHẾ ĐỘ TỐC ĐỘ]: CHỈ CẬP NHẬT NGÀY HÔM NAY ({today_str}) <<<")
    else:
        scrape_dates = [today_str, tomorrow_str]
        print(f">>> [CHẾ ĐỘ TIÊU CHUẨN]: CẬP NHẬT CẢ 2 NGÀY ({scrape_dates}) <<<")

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
            print(f" ####### BẮT ĐẦU QUÉT DỮ LIỆU NGÀY: {current_date} #######")
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

    # QUÉT FR24 ETA CHO TẤT CẢ CHUYẾN ĐẾN TRONG 16 TIẾNG TỚI
    print(f"\n=======================================================")
    print(f" BẮT ĐẦU QUÉT ETA TỪ FLIGHTRADAR24 CHO CÁC CHUYẾN ĐẾN (16H TỚI)")
    print(f"=======================================================")
    matched_count = 0

    for d, flights_list in all_flights_by_date.items():
        for f in flights_list:
            if f["direction"] == "Arrival":
                try:
                    flight_dt = tz_vn.localize(datetime.strptime(f"{f['date']} {f['scheduled']}", "%Y-%m-%d %H:%M"))
                    if check_start <= flight_dt <= window_limit:
                        eta = fetch_flight_eta_from_fr24(f["flight_no"])
                        if eta and eta != "--":
                            f["fr24_eta"] = eta
                            matched_count += 1
                            print(f"   -> [KHỚP FR24 ETA] {f['flight_no']} ({f['route']}): {eta}")
                        time.sleep(0.8) # Nghỉ nhẹ giữa các chuyến
                except Exception:
                    pass

    print(f"✓ Đã khớp thành công giờ ETA Flightradar24 cho {matched_count} chuyến bay!")

    # LƯU VÀO FIREBASE
    print(f"\n=======================================================")
    print(f" LƯU GÓI DỮ LIỆU TỐI ƯU VÀO FIREBASE")
    print(f"=======================================================")
    for d, flights_list in all_flights_by_date.items():
        if len(flights_list) > 0:
            try:
                db.collection("daily_schedules").document(d).set({
                    "date": d,
                    "flights": flights_list,
                    "updated_at": firestore.SERVER_TIMESTAMP
                })
                print(f"✓ Đã lưu thành công {len(flights_list)} chuyến bay ngày {d}.")
            except Exception as e:
                print(f"Lưu ý Firebase ngày {d}: {e}")

    print("=== ĐÃ HOÀN TẤT THU THẬP VÀ LƯU DỮ LIỆU THÀNH CÔNG! ===")

if __name__ == "__main__":
    scrape_viags()
