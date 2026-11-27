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

# Hàm lấy toàn bộ lịch bay đến SGN trực tiếp từ luồng tổng Flightradar24 (Không bị chặn)
def get_all_fr24_sgn_arrivals():
    eta_map = {}
    tz_vn = pytz.timezone('Asia/Ho_Chi_Minh')
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }

    # 1. Lấy toàn bộ lịch hạ cánh Tân Sơn Nhất (SGN) trên FR24 (2 trang đầu, bao quát 200 chuyến)
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

                        # Giờ ETA ước tính hoặc thực tế
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
            time.sleep(0.8)
        except Exception as e:
            print(f"Lưu ý khi đọc airport arrivals trang {page_num}: {e}")

    # 2. Quét luồng radar trực tiếp (Live Feed) của đội bay Vietnam Airlines (HVN)
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
    except Exception as e:
        print(f"Lưu ý khi đọc live feed FR24: {e}")

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

        # Lấy bản đồ ETA tổng từ Flightradar24
        fr24_map = get_all_fr24_sgn_arrivals()
        print(f"✓ Đã nạp được {len(fr24_map)} chuyến bay từ luồng Flightradar24.")

        docs_today = list(db.collection("flights").where("date", "==", today_str).stream())
        docs_tomorrow = list(db.collection("flights").where("date", "==", tomorrow_str).stream())
        all_docs = docs_today + docs_tomorrow

        updated_count = 0
        batch = db.batch()
        batch_count = 0

        for doc in all_docs:
            f = doc.to_dict()
            if f.get("direction") == "Arrival":
                try:
                    flight_dt = tz_vn.localize(datetime.strptime(f"{f['date']} {f['scheduled']}", "%Y-%m-%d %H:%M"))
                    # Bao quát trọn vẹn các chuyến bay trong vòng 16 tiếng tới
                    if check_start <= flight_dt <= window_limit:
                        f_no_clean = f.get("flight_no", "").replace(" ", "").upper()
                        if f_no_clean in fr24_map:
                            eta = fr24_map[f_no_clean]
                            batch.update(doc.reference, {
                                "fr24_eta": eta,
                                "updated_at": firestore.SERVER_TIMESTAMP
                            })
                            batch_count += 1
                            updated_count += 1
                            print(f"   -> [CẬP NHẬT FR24] {f_no_clean} ({f.get('route', '')}): {eta}")

                            if batch_count >= 400:
                                batch.commit()
                                batch = db.batch()
                                batch_count = 0
                except Exception:
                    pass

        if batch_count > 0:
            batch.commit()

        print(f"✓ Hoàn tất cập nhật ETA Flightradar24 cho {updated_count} chuyến bay trong vòng 16h!")
        print("=== KẾT THÚC CẬP NHẬT SIÊU TỐC FR24 ===")
        return

    # =======================================================
    # CHẾ ĐỘ TIÊU CHUẨN: CÀO VIAGS + QUÉT FLIGHTRADAR24
    # =======================================================
    if scrape_mode == "today_only":
        target_dates = [today_str]
        print(f">>> [CHẾ ĐỘ TỐC ĐỘ]: CHỈ CẬP NHẬT NGÀY HÔM NAY ({today_str}) <<<")
    else:
        target_dates = [today_str, tomorrow_str]
        print(f">>> [CHẾ ĐỘ TIÊU CHUẨN]: CẬP NHẬT CẢ 2 NGÀY ({target_dates}) <<<")

    all_flights = []

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

        for current_date in target_dates:
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
                            "fr24_eta": "--",
                            "updated_at": firestore.SERVER_TIMESTAMP
                        }
                        all_flights.append(flight_item)
                        section_vna += 1

                print(f"✓ [NGÀY {current_date}] LẤY ĐƯỢC: {section_vna} chuyến bay {sec['name']}.")
                page.close()

        browser.close()

    # KHỚP DỮ LIỆU TỪ LUỒNG TỔNG FLIGHTRADAR24 CHO TẤT CẢ CHUYẾN TRONG 16 TIẾNG TỚI
    print(f"\n=======================================================")
    print(f" BẮT ĐẦU CẬP NHẬT TẤT CẢ CHUYẾN ĐẾN TỪ FR24 TRONG 16H TỚI")
    print(f"=======================================================")
    
    fr24_map = get_all_fr24_sgn_arrivals()
    print(f"✓ Luồng Flightradar24 trả về {len(fr24_map)} chuyến bay hạ cánh SGN.")

    matched_fr24_count = 0
    for f in all_flights:
        if f["direction"] == "Arrival":
            try:
                flight_dt = tz_vn.localize(datetime.strptime(f"{f['date']} {f['scheduled']}", "%Y-%m-%d %H:%M"))
                # Bao quát toàn bộ chuyến bay trong 16 tiếng tới
                if check_start <= flight_dt <= window_limit:
                    f_no_clean = f["flight_no"].replace(" ", "").upper()
                    if f_no_clean in fr24_map:
                        f["fr24_eta"] = fr24_map[f_no_clean]
                        matched_fr24_count += 1
                        print(f"   -> [KHỚP FR24 ETA] {f_no_clean} ({f['route']}): {f['fr24_eta']}")
            except Exception:
                pass

    print(f"✓ Đã khớp thành công giờ ETA Flightradar24 cho {matched_fr24_count} chuyến bay!")

    # LƯU VÀO FIREBASE
    print(f"\n=======================================================")
    print(f" TỔNG CỘNG THU THẬP: {len(all_flights)} CHUYẾN BAY THẬT SGN")
    print(f"=======================================================")

    if len(all_flights) > 0:
        print("Đang dọn dẹp dữ liệu cũ của các ngày này trong Firebase...")
        for d in target_dates:
            docs = db.collection("flights").where("date", "==", d).stream()
            for doc in docs:
                doc.reference.delete()

        batch = db.batch()
        count = 0
        for flight in all_flights:
            doc_id = f"{flight['date']}_{flight['direction']}_{flight['flight_type']}_{flight['flight_no']}"
            doc_ref = db.collection("flights").document(doc_id)
            batch.set(doc_ref, flight)
            count += 1
            if count >= 400:
                batch.commit()
                batch = db.batch()
                count = 0

        if count > 0:
            batch.commit()
        print("=== ĐÃ LƯU THÀNH CÔNG TOÀN BỘ CHUYẾN BAY VÀO FIREBASE! ===")
    else:
        print("Cảnh báo: Không có dữ liệu để lưu.")

if __name__ == "__main__":
    scrape_viags()
