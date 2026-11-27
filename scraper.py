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

# Hàm click chọn dropdown mô phỏng người dùng
def select_dropdown_option(page, dropdown_index, option_name):
    print(f"-> Đang chọn ô số {dropdown_index + 1} sang: '{option_name}'...")
    try:
        # Bấm mở hộp chọn .nice-select theo thứ tự trên giao diện
        # 0: Flights | 1: Departures/Arrivals | 2: Location | 3: Time
        dropdowns = page.locator(".nice-select")
        if dropdowns.count() > dropdown_index:
            box = dropdowns.nth(dropdown_index)
            box.scroll_into_view_if_needed()
            box.click(timeout=4000)
            time.sleep(0.6)

            # Bấm vào dòng lựa chọn xuất hiện
            opt = page.locator(f"li.option:has-text('{option_name}'), li:has-text('{option_name}')").first
            opt.scroll_into_view_if_needed()
            opt.click(timeout=4000)
            time.sleep(3)
            print(f"   ✓ Đã chọn thành công: {option_name}")
            return True
    except Exception as e:
        print(f"   Lỗi khi click: {e}")

    # Dự phòng bằng lệnh JavaScript Click
    try:
        page.evaluate(f"""() => {{
            const lis = Array.from(document.querySelectorAll('li'));
            const target = lis.find(li => li.innerText.trim().toLowerCase() === '{option_name.lower()}');
            if (target) {{
                const p = target.closest('.nice-select');
                if (p) p.click();
                target.click();
            }}
        }}""")
        time.sleep(3)
        print(f"   ✓ JS Click dự phòng thành công cho: {option_name}")
        return True
    except Exception as ex:
        print(f"   Thất bại: {ex}")
        return False

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

        # BƯỚC 1: CHUYỂN LOCATION SANG HO CHI MINH (Ô số 3, index 2)
        print("2. Đang chọn Sân bay: Ho Chi Minh...")
        select_dropdown_option(page, 2, "Ho Chi Minh")
        time.sleep(2)

        # BƯỚC 2: CHỌN TIME SANG ALL DAY (Ô số 5, index 3 trong danh sách dropdown)
        select_dropdown_option(page, 3, "All day")
        time.sleep(1)

        # 4 Tổ hợp cần cào
        scenarios = [
            # 1. Quốc nội - Bay Đi
            {"f_type": "Domestic", "f_name": "Domestic Flights", "d_type": "Departure", "d_name": "Departures"},
            # 2. Quốc nội - Bay Đến
            {"f_type": "Domestic", "f_name": "Domestic Flights", "d_type": "Arrival", "d_name": "Arrivals"},
            # 3. Quốc tế - Bay Đi
            {"f_type": "International", "f_name": "International Flights", "d_type": "Departure", "d_name": "Departures"},
            # 4. Quốc tế - Bay Đến
            {"f_type": "International", "f_name": "International Flights", "d_type": "Arrival", "d_name": "Arrivals"},
        ]

        for sc in scenarios:
            f_type = sc["f_type"]
            d_type = sc["d_type"]
            print(f"\n=======================================================")
            print(f" ĐANG XỬ LÝ: [ {f_type.upper()} ] - [ {d_type.upper()} ]")
            print(f"=======================================================")

            # Chọn loại chuyến (Ô số 1, index 0)
            select_dropdown_option(page, 0, sc["f_name"])
            # Chọn chiều bay (Ô số 2, index 1)
            select_dropdown_option(page, 1, sc["d_name"])
            time.sleep(2)

            # BẤM NÚT VÀNG 'LOAD MORE' LIÊN TỤC CHO ĐẾN KHI HẾT SẠCH
            load_count = 0
            while True:
                load_more = page.get_by_text("Load more", exact=True)
                if load_more.count() > 0 and load_more.first.is_visible():
                    load_count += 1
                    print(f"-> Bấm 'Load more' lần thứ {load_count}...")
                    load_more.first.scroll_into_view_if_needed()
                    load_more.first.click()
                    time.sleep(2)
                    if load_count >= 20:
                        break
                else:
                    print(f"-> Đã tải xong toàn bộ danh sách (Đã bấm {load_count} lần 'Load more').")
                    break

            # Đọc tiêu đề các cột của bảng hiện tại
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
                    # Tuyến bay: Destination nếu Bay Đi, From nếu Bay Đến
                    route = col_map.get("destination", "") if d_type == "Departure" else col_map.get("from", "")

                    # Cổng (Gate): Chiều đi mới có Cổng
                    gate = col_map.get("gate", "--") if d_type == "Departure" else "--"
                    if not gate or gate == "":
                        gate = "--"

                    # Băng chuyền (Belt): Chiều đến mới có Băng chuyền
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
        # Xóa toàn bộ dữ liệu cũ ngày hôm nay trong Firebase để tránh xung đột
        print("Đang xóa sạch dữ liệu cũ trong Firebase...")
        docs = db.collection("flights").where("date", "==", today_str).stream()
        for doc in docs:
            doc.reference.delete()

        # Nạp dữ liệu mới sạch sẽ vào Firestore
        batch = db.batch()
        count = 0
        for flight in all_flights:
            # Document ID độc nhất: Ngày_ChiềuBay_LoạiChuyến_SốHiệu (Tuyệt đối không xung đột)
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
        print("=== ĐÃ LƯU THÀNH CÔNG DỮ LIỆU THẬT 100% VÀO FIREBASE! ===")
    else:
        print("Cảnh báo: Không có dữ liệu để lưu.")

if __name__ == "__main__":
    scrape_viags()
