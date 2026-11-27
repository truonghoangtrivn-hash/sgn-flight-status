import json
import os
import time
from datetime import datetime
import pytz
from playwright.sync_api import sync_playwright
import firebase_admin
from firebase_admin import credentials, firestore

# 1. Khởi tạo Firebase từ GitHub Secret
def init_firebase():
    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not sa_json:
        raise ValueError("Missing FIREBASE_SERVICE_ACCOUNT environment variable!")
    
    cred_dict = json.loads(sa_json)
    cred = credentials.Certificate(cred_dict)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    return firestore.client()

# 2. Thu thập dữ liệu từ trang web VIAGS
def scrape_viags():
    db = init_firebase()
    tz_vn = pytz.timezone('Asia/Ho_Chi_Minh')
    today_str = datetime.now(tz_vn).strftime('%Y-%m-%d')
    print(f"=== BẮT ĐẦU CÀO DỮ LIỆU NGÀY: {today_str} ===")

    all_flights = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        page = context.new_page()

        url = "https://www.viags.vn/en-US/flight-schedules"
        print(f"Đang truy cập: {url}")
        page.goto(url, wait_until="networkidle", timeout=60000)
        time.sleep(3)

        # Chọn sân bay Ho Chi Minh (SGN)
        try:
            page.locator("text='Ho Chi Minh'").click(timeout=5000)
            time.sleep(2)
        except Exception as e:
            print(f"Lưu ý khi chọn vị trí Hồ Chí Minh: {e}")

        # Các tổ hợp cần lấy dữ liệu
        flight_types = [("Domestic", "Domestic Flights"), ("International", "International Flights")]
        direction_types = [("Departure", "Departures"), ("Arrival", "Arrivals")]

        for f_code, f_name in flight_types:
            for d_code, d_name in direction_types:
                print(f"--- Đang tải dữ liệu: {f_code} - {d_code} ---")
                try:
                    # Bấm chọn loại chuyến bay
                    page.locator(f"text='{f_name}'").click(timeout=5000)
                    time.sleep(1)
                    # Bấm chọn hướng bay
                    page.locator(f"text='{d_name}'").click(timeout=5000)
                    time.sleep(2)

                    # Bấm 'Load more' nhiều lần nếu có thêm chuyến bay
                    for _ in range(10):
                        load_more = page.locator("text='Load more'")
                        if load_more.is_visible():
                            try:
                                load_more.click(timeout=2000)
                                time.sleep(1)
                            except:
                                break
                        else:
                            break

                    # Đọc bảng kết quả
                    rows = page.locator("table tbody tr").all()
                    print(f"Tìm thấy {len(rows)} hàng dữ liệu.")

                    for row in rows:
                        cols = [td.inner_text().strip() for td in row.locator("td").all()]
                        if len(cols) >= 6:
                            # Cột thông thường: Scheduled, Estimated, From/Dest, Airlines, FlightNo, Gate, Belt, Remarks
                            airline = cols[3] if len(cols) > 3 else ""
                            flight_no = cols[4] if len(cols) > 4 else ""

                            # Lọc chỉ lấy Vietnam Airlines hoặc mã chuyến VN/HVN
                            if "VIETNAM AIRLINES" in airline.upper() or flight_no.upper().startswith("VN"):
                                flight_data = {
                                    "date": today_str,
                                    "scheduled": cols[0],
                                    "estimated": cols[1] if len(cols) > 1 else "",
                                    "destination_or_from": cols[2] if len(cols) > 2 else "",
                                    "airline": airline,
                                    "flight_no": flight_no,
                                    "gate": cols[5] if len(cols) > 5 else "",
                                    "belt": cols[6] if len(cols) > 6 else "",
                                    "remarks": cols[7] if len(cols) > 7 else "",
                                    "flight_type": f_code,       # Domestic hoặc International
                                    "direction": d_code,           # Departure hoặc Arrival
                                    "airport": "SGN",
                                    "updated_at": firestore.SERVER_TIMESTAMP
                                }
                                all_flights.append(flight_data)

                except Exception as ex:
                    print(f"Lỗi khi cào mục {f_code} - {d_code}: {ex}")

        browser.close()

    print(f"Tổng số chuyến bay Vietnam Airlines tại SGN thu thập được: {len(all_flights)}")

    # 3. Lưu vào Firebase Firestore
    batch = db.batch()
    count = 0
    for flight in all_flights:
        # Tạo mã định danh tài liệu: VD: 2026-05-18_VN205_Departure
        doc_id = f"{flight['date']}_{flight['flight_no']}_{flight['direction']}"
        doc_ref = db.collection("flights").document(doc_id)
        batch.set(doc_ref, flight, merge=True)
        count += 1

        if count >= 400: # Firestore giới hạn 500 lệnh/batch
            batch.commit()
            batch = db.batch()
            count = 0

    if count > 0:
        batch.commit()
    print("=== LƯU THÀNH CÔNG VÀO FIREBASE! ===")

if __name__ == "__main__":
    scrape_viags()
