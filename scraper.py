import json
import os
import re
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
    try:
        select = page.locator(f"select:has(option:text('{option_text}'))").first
        if select.count() > 0:
            select.select_option(label=option_text)
            page.wait_for_timeout(1500)
    except Exception as e:
        print(f"Lưu ý khi chọn {option_text}: {e}")

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
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3000)

        # Chọn SGN
        select_dropdown(page, "Ho Chi Minh")

        combinations = [
            ("Domestic", "Domestic Flights", "Departure", "Departures"),
            ("Domestic", "Domestic Flights", "Arrival", "Arrivals"),
            ("International", "International Flights", "Departure", "Departures"),
            ("International", "International Flights", "Arrival", "Arrivals")
        ]

        for f_type, f_label, d_type, d_label in combinations:
            print(f"--- Đang lấy: {f_type} - {d_type} ---")
            select_dropdown(page, f_label)
            select_dropdown(page, d_label)

            # Bấm Load more nếu có
            for _ in range(8):
                try:
                    load_more = page.locator("a:has-text('Load more'), button:has-text('Load more')").first
                    if load_more.is_visible():
                        load_more.click(timeout=1500)
                        page.wait_for_timeout(800)
                    else:
                        break
                except:
                    break

            rows = page.locator("table tbody tr").all()

            for row in rows:
                cols = [td.inner_text().strip() for td in row.locator("td").all()]
                if not cols or len(cols) < 5:
                    continue

                row_text = " ".join(cols).upper()
                match = re.search(r'\b(VN|HVN)\s*([0-9]{2,4})\b', row_text)
                is_vna = "VIETNAM AIRLINES" in row_text or match is not None

                if is_vna:
                    # Lấy số hiệu chuyến bay
                    flight_no = f"VN{match.group(2)}" if match else (cols[5] if len(cols) > 5 else "VN---")
                    
                    # Nơi đi / nơi đến phụ thuộc vào Chiều bay (Departure / Arrival)
                    route = cols[3] if d_type == "Departure" else cols[2]

                    flight_data = {
                        "date": today_str,
                        "scheduled": cols[0] if len(cols) > 0 else "--:--",
                        "estimated": cols[1] if len(cols) > 1 else "--:--",
                        "from_city": cols[2] if len(cols) > 2 else "",
                        "to_city": cols[3] if len(cols) > 3 else "",
                        "route": route,
                        "airline": "Vietnam Airlines",
                        "flight_no": flight_no,
                        "gate": cols[6] if len(cols) > 6 else "--",
                        "belt": cols[7] if len(cols) > 7 else "--",
                        "remarks": cols[8] if len(cols) > 8 else (cols[-1] if len(cols) > 4 else "Normal"),
                        "flight_type": f_type,       # Domestic / International
                        "direction": d_type,           # Departure / Arrival
                        "airport": "SGN",
                        "fr24_url": f"https://www.flightradar24.com/data/flights/{flight_no.replace(' ', '')}",
                        "updated_at": firestore.SERVER_TIMESTAMP
                    }
                    all_flights.append(flight_data)

        browser.close()

    print(f"\n===> TỔNG CỘNG: {len(all_flights)} chuyến bay Vietnam Airlines.")

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
        print("=== ĐÃ LƯU DỮ LIỆU CHUẨN XÁC VÀO FIREBASE! ===")

if __name__ == "__main__":
    scrape_viags()
