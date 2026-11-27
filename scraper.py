import json
import os
import re
import time
from datetime import datetime
import pytz
from playwright.sync_api import sync_playwright
import firebase_admin
from firebase_admin import credentials, firestore

# 1. Khởi tạo Firebase
def init_firebase():
    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not sa_json:
        raise ValueError("Thiếu biến môi trường FIREBASE_SERVICE_ACCOUNT!")
    cred_dict = json.loads(sa_json)
    cred = credentials.Certificate(cred_dict)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    return firestore.client()

# Hàm đổi select và chờ hệ thống ASP.NET nạp xong
def change_select(page, selector, target_val):
    current = page.locator(selector).input_value()
    if current != target_val:
        print(f"-> Đổi {selector} sang '{target_val}'...")
        try:
            with page.expect_response(lambda r: "flight-schedules" in r.url and r.request.method == "POST", timeout=20000):
                page.select_option(selector, value=target_val)
            time.sleep(3)
        except Exception:
            page.select_option(selector, value=target_val)
            time.sleep(4)

# 2. Quy trình cào dữ liệu SGN
def scrape_viags():
    db = init_firebase()
    tz_vn = pytz.timezone('Asia/Ho_Chi_Minh')
    today_str = datetime.now(tz_vn).strftime('%Y-%m-%d')
    print(f"=======================================================")
    print(f" BẮT ĐẦU CÀO TOÀN BỘ CHUYẾN BAY THẬT TÂN SƠN NHẤT (SGN)")
    print(f" NGÀY: {today_str}")
    print(f"=======================================================")

    all_flights = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900}
        )
        page = context.new_page()

        url = "https://www.viags.vn/en-US/flight-schedules"
        print(f"1. Mở trang: {url}")
        page.goto(url, wait_until="networkidle", timeout=60000)
        time.sleep(3)

        SEL_ORIGIN = "#ctl00_mainContent_ctl00_ddlOriginal"
        SEL_STATION = "#ctl00_mainContent_ctl00_ddlStation"
        SEL_TRIP = "#ctl00_mainContent_ctl00_ddlTrip"

        # BƯỚC 1: Chọn sân bay Sài Gòn (SGN)
        print("2. Đang chọn Sân bay: Ho Chi Minh (SGN)...")
        change_select(page, SEL_ORIGIN, "SGN")

        tasks = [
            {"station": "DOM", "trip": "D", "f_type": "Domestic", "d_type": "Departure"},
            {"station": "DOM", "trip": "A", "f_type": "Domestic", "d_type": "Arrival"},
            {"station": "INT", "trip": "D", "f_type": "International", "d_type": "Departure"},
            {"station": "INT", "trip": "A", "f_type": "International", "d_type": "Arrival"}
        ]

        for task in tasks:
            f_type = task["f_type"]
            d_type = task["d_type"]
            print(f"\n=======================================================")
            print(f" ĐANG XỬ LÝ: [ {f_type.upper()} ] - [ {d_type.upper()} ] TẠI SGN")
            print(f"=======================================================")

            # Đổi loại chuyến và chiều bay
            change_select(page, SEL_STATION, task["station"])
            change_select(page, SEL_TRIP, task["trip"])
            time.sleep(3) # Chờ bảng mới ổn định

            # BẤM NÚT VÀNG 'LOAD MORE' LIÊN TỤC CHO ĐẾN KHI HẾT SẠCH
            load_count = 0
            empty_retry = 0
            while True:
                # Cuộn chuột xuống đáy trang để ép hiển thị nút Load more
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(1)

                load_more = page.locator(".c-btn1:not(.hidden):visible, span.c-btn1:not(.hidden):visible")
                
                if load_more.count() > 0 and load_more.first.is_visible():
                    empty_retry = 0
                    load_count += 1
                    print(f"-> Bấm 'Load more' lần {load_count}...")
                    try:
                        load_more.first.scroll_into_view_if_needed()
                        load_more.first.click(timeout=3000)
                        time.sleep(2.5) # Đợi nạp thêm chuyến bay
                    except Exception as e:
                        time.sleep(2)
                    
                    if load_count >= 25:
                        break
                else:
                    # Kiểm tra lại lần 2 để chắc chắn rằng đã tải hết sạch thật sự
                    empty_retry += 1
                    if empty_retry >= 2:
                        print(f"-> Đã tải xong toàn bộ danh sách (đã bấm {load_count} lần 'Load more').")
                        break
                    time.sleep(2)

            # Đọc tiêu đề các cột
            headers = [th.inner_text().strip().lower() for th in page.locator(".c-table1 table thead tr th, table thead tr th").all()]
            rows = page.locator(".c-table1 table tbody tr, table tbody tr").all()
            print(f"Tiêu đề bảng: {headers}")
            print(f"Tổng số dòng cào được: {len(rows)}")

            section_vna_count = 0
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

                # Lọc chuyến bay của Vietnam Airlines
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
                    route = col_map.get("destination", "") if d_type == "Departure" else col_map.get("from", "")
                    gate = col_map.get("gate", "--") if d_type == "Departure" else "--"
                    if not gate or gate == "":
                        gate = "--"

                    belt = col_map.get("baggage belts", col_map.get("belt", "--")) if d_type == "Arrival" else "--"
                    if not belt or belt == "":
                        belt = "--"

                    remarks = col_map.get("remarks", "Normal")
                    if not remarks:
                        remarks = "Normal"

                    flight_item = {
                        "date": today_str,
                        "scheduled": col_map.get("scheduled", "--:--"),
                        "estimated": col_map.get("estimated", col_map.get("scheduled", "--:--")),
                        "destination_or_from": route,
                        "route": route,
                        "airline": "Vietnam Airlines",
                        "flight_no": flight_no,
                        "gate": gate,
                        "belt": belt,
                        "remarks": remarks,
                        "flight_type": f_type,       # Domestic / International
                        "direction": d_type,           # Departure / Arrival
                        "airport": "SGN",
                        "fr24_url": f"https://www.flightradar24.com/data/flights/{flight_no}",
                        "updated_at": firestore.SERVER_TIMESTAMP
                    }
                    all_flights.append(flight_item)
                    section_vna_count += 1

            print(f"-> Thu thập được {section_vna_count} chuyến bay Vietnam Airlines mục này.")

        browser.close()

    print(f"\n=======================================================")
    print(f" TỔNG CỘNG THU THẬP: {len(all_flights)} CHUYẾN BAY THẬT SGN")
    print(f"=======================================================")

    if len(all_flights) > 0:
        print("Đang dọn dẹp dữ liệu cũ trong Firebase...")
        docs = db.collection("flights").where("date", "==", today_str).stream()
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
        print("=== ĐÃ LƯU THÀNH CÔNG DỮ LIỆU THẬT SGN VÀO FIREBASE! ===")
    else:
        print("Cảnh báo: Không có dữ liệu để lưu.")

if __name__ == "__main__":
    scrape_viags()
