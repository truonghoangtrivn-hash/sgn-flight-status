import json
import os
import re
import time
from datetime import datetime
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

# Hàm đổi giá trị dropdown và chờ trình duyệt tải xong mạng
def change_dropdown_and_wait(page, select_locator, target_text):
    print(f"-> Đang chuyển lựa chọn sang: '{target_text}'...")
    try:
        # Chọn giá trị bằng Playwright select_option
        select_locator.select_option(label=target_text)
    except:
        # Nếu không chọn được bằng nhãn, chọn bằng value
        select_locator.select_option(value=target_text)

    # Chờ trang hoàn tất việc gửi nhận dữ liệu mạng (lệnh POST)
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except:
        pass
    time.sleep(3)

# 2. Quy trình cào dữ liệu toàn diện
def scrape_viags():
    db = init_firebase()
    tz_vn = pytz.timezone('Asia/Ho_Chi_Minh')
    today_str = datetime.now(tz_vn).strftime('%Y-%m-%d')
    print(f"=======================================================")
    print(f" BẮT ĐẦU CÀO TOÀN BỘ CHUYẾN BAY TSN (SGN): {today_str}")
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

        # Định vị chính xác 4 ô select trên giao diện
        sel_location = page.locator("select:has(option:text('Ho Chi Minh')), select:has(option[value='SGN'])").first
        sel_flight = page.locator("select:has(option:text('Domestic Flights')), select:has(option[value='DOM'])").first
        sel_direction = page.locator("select:has(option:text('Departures')), select:has(option:text('Arrivals'))").first

        # BƯỚC 1: Chọn sân bay Sài Gòn (Ho Chi Minh)
        print("2. Đang chọn sân bay Hồ Chí Minh...")
        change_dropdown_and_wait(page, sel_location, "Ho Chi Minh")

        # 4 Mục cần lấy dữ liệu
        categories = [
            {"f_name": "Domestic Flights", "d_name": "Departures", "f_type": "Domestic", "d_type": "Departure"},
            {"f_name": "Domestic Flights", "d_name": "Arrivals", "f_type": "Domestic", "d_type": "Arrival"},
            {"f_name": "International Flights", "d_name": "Departures", "f_type": "International", "d_type": "Departure"},
            {"f_name": "International Flights", "d_name": "Arrivals", "f_type": "International", "d_type": "Arrival"},
        ]

        for cat in categories:
            f_type = cat["f_type"]
            d_type = cat["d_type"]
            print(f"\n=======================================================")
            print(f" XỬ LÝ: [ {f_type.upper()} ] - [ {d_type.upper()} ]")
            print(f"=======================================================")

            # Chọn loại chuyến bay
            change_dropdown_and_wait(page, sel_flight, cat["f_name"])
            # Chọn chiều bay
            change_dropdown_and_wait(page, sel_direction, cat["d_name"])

            # BƯỚC QUAN TRỌNG: BẤM NÚT VÀNG 'LOAD MORE' LIÊN TỤC CHO ĐẾN KHI HẾT SẠCH
            load_count = 0
            while True:
                # Tìm nút màu vàng Load more
                load_more_btn = page.locator("button:has-text('Load more'), a:has-text('Load more'), .btn:has-text('Load more')").first
                if load_more_btn.is_visible():
                    load_count += 1
                    print(f"-> Bấm 'Load more' lần thứ {load_count}...")
                    load_more_btn.scroll_into_view_if_needed()
                    load_more_btn.click()
                    time.sleep(2)
                    try:
                        page.wait_for_load_state("networkidle", timeout=5000)
                    except:
                        pass
                else:
                    print(f"-> Đã tải xong toàn bộ danh sách (đã bấm {load_count} lần Load more).")
                    break

            # Đọc tiêu đề các cột của bảng
            headers = [th.inner_text().strip().lower() for th in page.locator("table thead tr th").all()]
            rows = page.locator("table tbody tr").all()
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

                # Nhận diện chuyến bay của Vietnam Airlines
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
                    # Lấy tuyến bay: Destination nếu Bay Đi, From nếu Bay Đến
                    route = col_map.get("destination", "") if d_type == "Departure" else col_map.get("from", "")

                    # Cổng (Gate): Chiều đi mới có Cổng, chiều đến để '--'
                    gate = col_map.get("gate", "--") if d_type == "Departure" else "--"
                    if not gate or gate == "":
                        gate = "--"

                    # Băng chuyền (Belt): Chiều đến mới có Băng chuyền, chiều đi để '--'
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
                        "flight_type": f_type,
                        "direction": d_type,
                        "airport": "SGN",
                        "fr24_url": f"https://www.flightradar24.com/data/flights/{flight_no}",
                        "updated_at": firestore.SERVER_TIMESTAMP
                    }
                    all_flights.append(flight_item)
                    section_vna_count += 1

            print(f"-> Thu thập được {section_vna_count} chuyến bay Vietnam Airlines.")

        browser.close()

    print(f"\n=======================================================")
    print(f" TỔNG CỘNG THU THẬP: {len(all_flights)} CHUYẾN BAY THẬT SGN")
    print(f"=======================================================")

    if len(all_flights) > 0:
        # Xóa sạch dữ liệu cũ ngày hôm nay
        print("Đang làm sạch dữ liệu cũ trong Firebase...")
        docs = db.collection("flights").where("date", "==", today_str).stream()
        for doc in docs:
            doc.reference.delete()

        # Lưu dữ liệu mới hoàn chỉnh
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

if __name__ == "__main__":
    scrape_viags()
