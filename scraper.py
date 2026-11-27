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

# Đổi select và chờ phản hồi POST từ máy chủ VIAGS
def change_select(page, selector, target_val):
    current = page.locator(selector).input_value()
    if current != target_val:
        print(f"-> Đổi {selector} sang '{target_val}'...")
        try:
            with page.expect_response(lambda r: "flight-schedules" in r.url and r.request.method == "POST", timeout=25000):
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

        # 4 Tổ hợp cần lấy dữ liệu
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

            # Chuyển loại chuyến và chiều bay
            change_select(page, SEL_STATION, task["station"])
            change_select(page, SEL_TRIP, task["trip"])
            time.sleep(3)

            # BƯỚC QUAN TRỌNG: BẤM NÚT 'LOAD MORE' LIÊN TỤC BẰNG LỆNH JS
            load_count = 0
            for i in range(25):
                # Kiểm tra nút vàng còn bấm được hay không
                has_more = page.evaluate("""() => {
                    const btn = document.querySelector('.c-btn1');
                    if (!btn) return false;
                    if (btn.classList.contains('hidden')) return false;
                    const style = window.getComputedStyle(btn);
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                    
                    // Thực hiện click vào nút
                    btn.click();
                    return true;
                }""")

                if has_more:
                    load_count += 1
                    print(f"   -> Bấm 'Load more' lần {load_count}...")
                    time.sleep(1.5)
                else:
                    print(f"   -> Đã tải xong toàn bộ danh sách (đã bấm {load_count} lần 'Load more').")
                    break

            # Đọc tiêu đề các cột của bảng
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

                # Nhận diện chuyến bay Vietnam Airlines
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
            # Document ID độc nhất: Ngày_ChiềuBay_LoạiChuyến_SốHiệu
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
        print("=== ĐÃ LƯU THÀNH CÔNG TOÀN BỘ CHUYẾN BAY THẬT VÀO FIREBASE! ===")
    else:
        print("Cảnh báo: Không có dữ liệu để lưu.")

if __name__ == "__main__":
    scrape_viags()
