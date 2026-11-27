import json
import os
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

def select_dropdown(page, option_text):
    """Chọn giá trị trong dropdown bằng text hiển thị"""
    try:
        select = page.locator(f"select:has(option:text('{option_text}'))").first
        if select.count() > 0:
            select.select_option(label=option_text)
            page.wait_for_timeout(1500)
            print(f"-> Đã chọn dropdown: {option_text}")
    except Exception as e:
        print(f"Lỗi khi chọn {option_text}: {e}")

# 2. Cào dữ liệu VIAGS
def scrape_viags():
    db = init_firebase()
    tz_vn = pytz.timezone('Asia/Ho_Chi_Minh')
    today_str = datetime.now(tz_vn).strftime('%Y-%m-%d')
    print(f"=== BẮT ĐẦU CÀO DỮ LIỆU SGN CHO NGÀY: {today_str} ===")

    all_flights = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        url = "https://www.viags.vn/en-US/flight-schedules"
        print(f"Đang mở trang: {url}")
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3000)

        # 1. Chọn sân bay Hồ Chí Minh (SGN)
        select_dropdown(page, "Ho Chi Minh")

        combinations = [
            ("Domestic", "Domestic Flights", "Departure", "Departures"),
            ("Domestic", "Domestic Flights", "Arrival", "Arrivals"),
            ("International", "International Flights", "Departure", "Departures"),
            ("International", "International Flights", "Arrival", "Arrivals")
        ]

        for f_type, f_label, d_type, d_label in combinations:
            print(f"\n--- Đang xử lý: {f_type} - {d_type} ---")
            select_dropdown(page, f_label)
            select_dropdown(page, d_label)

            # Bấm nút Search/Tìm kiếm nếu có
            try:
                search_btn = page.locator("button:has-text('Search'), input[type='submit'], .btn-search").first
                if search_btn.is_visible():
                    search_btn.click()
                    page.wait_for_timeout(2000)
            except:
                pass

            # Bấm 'Load more' nhiều lần để lấy toàn bộ danh sách
            for i in range(12):
                try:
                    load_more = page.locator("a:has-text('Load more'), button:has-text('Load more')").first
                    if load_more.is_visible():
                        load_more.click(timeout=2000)
                        page.wait_for_timeout(1000)
                    else:
                        break
                except:
                    break

            # Thu thập các dòng trong bảng
            rows = page.locator("table tbody tr").all()
            print(f"Số dòng tìm thấy trong bảng: {len(rows)}")

            for row in rows:
                cols = [td.inner_text().strip() for td in row.locator("td").all()]
                if len(cols) >= 5:
                    airline = cols[3] if len(cols) > 3 else ""
                    flight_no = cols[4] if len(cols) > 4 else ""

                    # Lọc Vietnam Airlines hoặc mã chuyến VN
                    is_vna = "VIETNAM AIRLINES" in airline.upper() or flight_no.upper().startswith("VN")
                    if is_vna:
                        flight_data = {
                            "date": today_str,
                            "scheduled": cols[0],
                            "estimated": cols[1] if len(cols) > 1 else "",
                            "destination_or_from": cols[2] if len(cols) > 2 else "",
                            "airline": airline if airline else "VIETNAM AIRLINES",
                            "flight_no": flight_no,
                            "gate": cols[5] if len(cols) > 5 else "",
                            "belt": cols[6] if len(cols) > 6 else "",
                            "remarks": cols[7] if len(cols) > 7 else "",
                            "flight_type": f_type,     # Domestic / International
                            "direction": d_type,         # Departure / Arrival
                            "airport": "SGN",
                            "updated_at": firestore.SERVER_TIMESTAMP
                        }
                        all_flights.append(flight_data)

        browser.close()

    print(f"\n===> TỔNG CỘNG LẤY ĐƯỢC: {len(all_flights)} CHUYẾN BAY VIETNAM AIRLINES TẠI SGN <===")

    if len(all_flights) > 0:
        batch = db.batch()
        count = 0
        for flight in all_flights:
            doc_id = f"{flight['date']}_{flight['flight_no']}_{flight['direction']}"
            doc_ref = db.collection("flights").document(doc_id)
            batch.set(doc_ref, flight, merge=True)
            count += 1
            if count >= 400:
                batch.commit()
                batch = db.batch()
                count = 0
        if count > 0:
            batch.commit()
        print("=== ĐÃ LƯU THÀNH CÔNG TẤT CẢ VÀO FIREBASE! ===")
    else:
        print("Không có chuyến bay nào để lưu.")

if __name__ == "__main__":
    scrape_viags()
