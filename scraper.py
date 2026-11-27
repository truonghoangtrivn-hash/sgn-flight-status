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

# Hàm click chuột trực tiếp vào chữ hiển thị trên giao diện (Chuẩn 100% thao tác người dùng)
def select_by_visible_text(page, current_text, target_text):
    print(f"-> Thao tác: Bấm [{current_text}] -> Chọn [{target_text}]")
    try:
        # 1. Bấm vào hộp đang hiển thị chữ hiện tại trong khung FLIGHT INFORMATION
        box = page.locator(f":text-is('{current_text}')").first
        box.scroll_into_view_if_needed()
        box.click(timeout=5000)
        time.sleep(1)

        # 2. Bấm vào giá trị mục tiêu trong danh sách rơi xuống
        target = page.locator(f":text-is('{target_text}')").last
        target.scroll_into_view_if_needed()
        target.click(timeout=5000)
        time.sleep(3.5) # Đợi trang nạp dữ liệu xong
        print(f"   ✓ Đã chọn thành công: {target_text}")
        return True
    except Exception as e:
        print(f"   Lưu ý khi bấm [{target_text}]: {e}")
        return False

# 2. Thu thập dữ liệu chuyến bay
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

        # BƯỚC 1: BẤM CHỌN SÂN BAY HO CHI MINH (SGN)
        print("2. Đang chuyển từ Ha Noi sang Ho Chi Minh...")
        select_by_visible_text(page, "Ha Noi", "Ho Chi Minh")

        # 4 Kịch bản cào dữ liệu
        categories = [
            # 1. Quốc nội - Bay Đi
            {"f_type": "Domestic", "d_type": "Departure", "f_switch": None, "d_switch": None},
            # 2. Quốc nội - Bay Đến
            {"f_type": "Domestic", "d_type": "Arrival", "f_switch": None, "d_switch": ("Departures", "Arrivals")},
            # 3. Quốc tế - Bay Đi
            {"f_type": "International", "d_type": "Departure", "f_switch": ("Domestic Flights", "International Flights"), "d_switch": ("Arrivals", "Departures")},
            # 4. Quốc tế - Bay Đến
            {"f_type": "International", "d_type": "Arrival", "f_switch": None, "d_switch": ("Departures", "Arrivals")},
        ]

        for cat in categories:
            f_type = cat["f_type"]
            d_type = cat["d_type"]
            print(f"\n=======================================================")
            print(f" ĐANG XỬ LÝ: [ {f_type.upper()} ] - [ {d_type.upper()} ]")
            print(f"=======================================================")

            # Chuyển đổi loại chuyến nếu cần
            if cat["f_switch"]:
                select_by_visible_text(page, cat["f_switch"][0], cat["f_switch"][1])

            # Chuyển đổi chiều bay nếu cần
            if cat["d_switch"]:
                select_by_visible_text(page, cat["d_switch"][0], cat["d_switch"][1])

            # BƯỚC QUAN TRỌNG: BẤM NÚT MÀU VÀNG "LOAD MORE" CHO ĐẾN KHI HẾT SẠCH
            load_count = 0
            while True:
                # Tìm chữ "Load more" xuất hiện trên màn hình
                load_more = page.get_by_text("Load more", exact=True)
                if load_more.count() > 0 and load_more.first.is_visible():
                    load_count += 1
                    print(f"-> Bấm nút vàng 'Load more' lần thứ {load_count}...")
                    load_more.first.scroll_into_view_if_needed()
                    load_more.first.click()
                    time.sleep(2)
                else:
                    print(f"-> Đã tải xong toàn bộ chuyến bay (Đã bấm {load_count} lần 'Load more').")
                    break

            # Đọc tiêu đề các cột của bảng
            headers = [th.inner_text().strip().lower() for th in page.locator("table thead tr th").all()]
            rows = page.locator("table tbody tr").all()
            print(f"Cột bảng: {headers}")
            print(f"Tổng số dòng tìm thấy: {len(rows)}")

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

                # Nhận diện chuyến bay của Vietnam Airlines (Mã VN hoặc hãng Vietnam Airlines)
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
        # Xóa dữ liệu cũ của ngày hôm nay
        print("Đang làm sạch dữ liệu cũ trong Firebase...")
        docs = db.collection("flights").where("date", "==", today_str).stream()
        for doc in docs:
            doc.reference.delete()

        # Lưu dữ liệu sạch mới vào Firestore
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
        print("Cảnh báo: Không có chuyến bay nào được lưu.")

if __name__ == "__main__":
    scrape_viags()
