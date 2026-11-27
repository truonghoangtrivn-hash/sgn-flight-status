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

# Hàm lấy dữ liệu từ Flightradar24
def get_all_fr24_sgn_arrivals():
    eta_map = {}
    tz_vn = pytz.timezone('Asia/Ho_Chi_Minh')
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }

    # 1. Quét lịch hạ cánh Tân Sơn Nhất (SGN)
    for page_num in [1, 2]:
        try:
            url = f"https://api.flightradar24.com/common/v1/airport.json?code=sgn&plugin[]=&plugin-setting[schedule][mode]=arrivals&page={page_num}&limit=100"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode('utf-8'))
                    arrivals = data.get("result", {}).get("response", {}).get("airport", {}).get("pluginData", {}).get("schedule", {}).get("arrivals", {}).get("data", [])
                    for item in arrivals:
                        f_info = item.get("flight", {})
                        f_no = f_info.get("identification", {}).get("number", {}).get("default", "")
                        if not f_no:
                            continue
                        f_no_clean = f_no.replace(" ", "").upper()
                        time_info = f_info.get("time", {})

                        est_ts = time_info.get("estimated", {}).get("arrival")
                        real_ts = time_info.get("real", {}).get("arrival")
                        target_ts = est_ts or real_ts

                        if target_ts:
                            dt = datetime.fromtimestamp(target_ts, tz_vn)
                            eta_map[f_no_clean] = dt.strftime('%H:%M')
                        else:
                            status_text = f_info.get("status", {}).get("text", "")
                            match = re.search(r'(\d{1,2}:\d{2}\s*(?:AM|PM)?)', status_text, re.I)
                            if match:
                                t_str = match.group(1).strip()
                                try:
                                    t_obj = datetime.strptime(t_str, "%I:%M %p")
                                    eta_map[f_no_clean] = t_obj.strftime("%H:%M")
                                except Exception:
                                    eta_map[f_no_clean] = t_str
            time.sleep(0.5)
        except Exception as e:
            pass

    # 2. Quét radar trực tiếp (Live Feed) đội bay Vietnam Airlines
    try:
        feed_url = "https://data-cloud.flightradar24.com/zones/fcgi/feed.js?airline=HVN"
        req_feed = urllib.request.Request(feed_url, headers=headers)
        with urllib.request.urlopen(req_feed, timeout=10) as resp:
            if resp.status == 200:
                feed_data = json.loads(resp.read().decode('utf-8'))
                for k, v in feed_data.items():
                    if isinstance(v, list) and len(v) >= 14:
                        callsign = str(v[16] or v[13] or "")
                        dest = str(v[12] or "")
                        eta_ts = v[10]
                        if dest == "SGN" and eta_ts:
                            dt = datetime.fromtimestamp(eta_ts, tz_vn)
                            eta_str = dt.strftime('%H:%M')
                            vn_no = callsign.replace("HVN", "VN").replace(" ", "").upper()
                            eta_map[vn_no] = eta_str
    except Exception:
        pass

    return eta_map

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
    # CHẾ ĐỘ SIÊU TỐC: CHỈ QUÉT FLIGHTRADAR24 (CHẠY TRONG 20S)
    # =======================================================
    if scrape_mode == "fr24_only":
        print(f"=======================================================")
        print(f" >>> [CHẾ ĐỘ SIÊU TỐC]: CẬP NHẬT FLIGHTRADAR24 (16H TỚI) <<<")
        print(f"=======================================================")

        fr24_map = get_all_fr24_sgn_arrivals()
        print(f"✓ Đã nạp được {len(fr24_map)} chuyến bay từ luồng Flightradar24.")

        # Đọc dữ liệu từ gói tài liệu ngày hôm nay và ngày mai (Chỉ tốn đúng 2 lượt đọc!)
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
                                    f_no = f.get("flight_no", "").replace(" ", "").upper()
                                    if f_no in fr24_map:
                                        f["fr24_eta"] = fr24_map[f_no]
                                        updated = True
                                        print(f"   -> [CẬP NHẬT FR24] {f_no}: {f['fr24_eta']}")
                            except Exception:
                                pass
                    if updated:
                        # Ghi lại đúng 1 lần duy nhất cho toàn bộ ngày
                        doc_ref.set({"date": d, "flights": flights_list, "updated_at": firestore.SERVER_TIMESTAMP})
            except Exception as e:
                print(f"Lưu ý Firebase: {e}")

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

    # KHỚP FR24 ETA CHO TẤT CẢ CHUYẾN TRONG 16 TIẾNG TỚI
    print(f"\n=======================================================")
    print(f" CẬP NHẬT TẤT CẢ CHUYẾN ĐẾN TỪ FR24 TRONG 16H TỚI")
    print(f"=======================================================")
    fr24_map = get_all_fr24_sgn_arrivals()
    print(f"✓ Luồng Flightradar24 trả về {len(fr24_map)} chuyến bay.")

    for d, flights_list in all_flights_by_date.items():
        for f in flights_list:
            if f["direction"] == "Arrival":
                try:
                    flight_dt = tz_vn.localize(datetime.strptime(f"{f['date']} {f['scheduled']}", "%Y-%m-%d %H:%M"))
                    if check_start <= flight_dt <= window_limit:
                        f_no = f["flight_no"].replace(" ", "").upper()
                        if f_no in fr24_map:
                            f["fr24_eta"] = fr24_map[f_no]
                            print(f"   -> [KHỚP FR24 ETA] {f_no} ({f['route']}): {f['fr24_eta']}")
                except Exception:
                    pass

    # LƯU VÀO FIREBASE THEO GÓI NGÀY (CHỈ TỐN ĐÚNG 1 LƯỢT GHI MỖI NGÀY - KHÔNG LO 429!)
    print(f"\n=======================================================")
    print(f" LƯU GÓI DỮ LIỆU TỐI ƯU VÀO FIREBASE")
    print(f"=======================================================")
    for d, flights_list in all_flights_by_date.items():
        if len(flights_list) > 0:
            try:
                # 1. Lưu gói tổng vào collection 'daily_schedules' (Chỉ 1 lượt ghi!)
                db.collection("daily_schedules").document(d).set({
                    "date": d,
                    "flights": flights_list,
                    "updated_at": firestore.SERVER_TIMESTAMP
                })
                print(f"✓ Đã lưu thành công {len(flights_list)} chuyến bay ngày {d} (chỉ tốn 1 lượt ghi).")
            except Exception as e:
                print(f"Lưu ý khi ghi Firebase: {e}")

    print("=== ĐÃ HOÀN TẤT THU THẬP VÀ LƯU DỮ LIỆU THÀNH CÔNG! ===")

if __name__ == "__main__":
    scrape_viags()
