import json
import os
import re
import time
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

    # Đọc chế độ từ GitHub Action (today_only hoặc both_days)
    scrape_mode = os.environ.get("SCRAPE_MODE", "both_days")
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

                # 1. Điền ngày cần cào
                page.evaluate(f"""(targetDate) => {{
                    const dateInp = document.querySelector('{SEL_DATE}, [name$="datetimepicker1"]');
                    if (dateInp) {{
                        dateInp.value = targetDate;
                        if (window.jQuery) window.jQuery(dateInp).trigger('change');
                        dateInp.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    }}
                }}""", current_date)
                time.sleep(1)

                # 2. Luôn chọn Sân bay Sài Gòn (SGN)
                print(f"1. Chọn Sân bay: Ho Chi Minh (SGN) cho ngày {current_date}...")
                select_and_wait(page, SEL_ORIGIN, "SGN")

                # 3. Chọn Loại chuyến (DOM / INT)
                if sec["station"] != "DOM":
                    print(f"2. Chọn Loại chuyến: {sec['station']}...")
                    select_and_wait(page, SEL_STATION, sec["station"])

                # 4. Chọn Chiều bay (D / A)
                if sec["trip"] != "D":
                    print(f"3. Chọn Chiều bay: {sec['trip']}...")
                    select_and_wait(page, SEL_TRIP, sec["trip"])

                # 5. BẤM NÚT VÀNG 'LOAD MORE' LIÊN TỤC CHO ĐẾN HẾT SẠCH
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

                # Đọc dữ liệu từ bảng
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
                            "updated_at": firestore.SERVER_TIMESTAMP
                        }
                        all_flights.append(flight_item)
                        section_vna += 1

                print(f"✓ [NGÀY {current_date}] LẤY ĐƯỢC: {section_vna} chuyến bay {sec['name']}.")
                page.close()

        browser.close()

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
        print("=== ĐÃ LƯU THÀNH CÔNG VÀO FIREBASE! ===")
    else:
        print("Cảnh báo: Không có dữ liệu để lưu.")

if __name__ == "__main__":
    scrape_viags()
