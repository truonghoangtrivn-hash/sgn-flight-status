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

# Hàm click chuột trực tiếp vào menu thả xuống theo giao diện thực tế
def click_nice_select(page, current_or_label_text, target_option_text):
    print(f"Đang đổi lựa chọn sang: '{target_option_text}'...")
    try:
        # 1. Tìm và bấm vào hộp chọn hiển thị trên màn hình
        dropdown = page.locator(f".nice-select:has-text('{current_or_label_text}')").first
        if not dropdown.is_visible():
            dropdown = page.locator(f"div.nice-select:has(span:has-text('{current_or_label_text}'))").first
        
        dropdown.scroll_into_view_if_needed()
        dropdown.click(timeout=5000)
        time.sleep(0.8)

        # 2. Bấm vào giá trị cần chọn trong danh sách thả xuống
        option = page.locator(f"li.option:has-text('{target_option_text}'), li:has-text('{target_option_text}')").first
        option.scroll_into_view_if_needed()
        option.click(timeout=5000)
        time.sleep(3.5) # Chờ bảng tải dữ liệu mới
        print(f"-> Đã chọn thành công: {target_option_text}")
        return True
    except Exception as e:
        print(f"Click trực tiếp '{target_option_text}' gặp lỗi: {e}")
        # Phương án dự phòng bằng JavaScript click
        try:
            page.evaluate(f"""() => {{
                const opts = Array.from(document.querySelectorAll('.nice-select ul li'));
                const target = opts.find(li => li.innerText.trim().toLowerCase() === '{target_option_text.lower()}');
                if (target) {{
                    target.closest('.nice-select').click();
                    target.click();
                }}
            }}""")
            time.sleep(3.5)
            print(f"-> JS Click dự phòng thành công cho: {target_option_text}")
            return True
        except Exception as ex:
            print(f"Không thể chọn {target_option_text}: {ex}")
            return False

# 2. Cào dữ liệu toàn bộ chuyến bay SGN
def scrape_viags():
    db = init_firebase()
    tz_vn = pytz.timezone('Asia/Ho_Chi_Minh')
    today_str = datetime.now(tz_vn).strftime('%Y-%m-%d')
    print(f"=======================================================")
    print(f" BẮT ĐẦU CÀO DỮ LIỆU CHUẨN XÁC VIAGS SGN: {today_str}")
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
        print(f"Mở trang web: {url}")
        page.goto(url, wait_until="networkidle", timeout=60000)
        time.sleep(3)

        # BƯỚC 1: Chuyển Location sang 'Ho Chi Minh' (Mặc định trang ban đầu là Ha Noi)
        click_nice_select(page, "Ha Noi", "Ho Chi Minh")

        # 4 Tổ hợp cần cào
        scenarios = [
            # 1. Quốc nội - Bay Đi
            {"f_curr": "Domestic Flights", "f_target": "Domestic Flights", "d_curr": "Departures", "d_target": "Departures", "f_type": "Domestic", "d_type": "Departure"},
            # 2. Quốc nội - Bay Đến
            {"f_curr": "Domestic Flights", "f_target": "Domestic Flights", "d_curr": "Departures", "d_target": "Arrivals", "f_type": "Domestic", "d_type": "Arrival"},
            # 3. Quốc tế - Bay Đi
            {"f_curr": "Domestic Flights", "f_target": "International Flights", "d_curr": "Arrivals", "d_target": "Departures", "f_type": "International", "d_type": "Departure"},
            # 4. Quốc tế - Bay Đến
            {"f_curr": "International Flights", "f_target": "International Flights", "d_curr": "Departures", "d_target": "Arrivals", "f_type": "International", "d_type": "Arrival"}
        ]

        for sc in scenarios:
            f_type = sc["f_type"]
            d_type = sc["d_type"]
            print(f"\n=======================================================")
            print(f" ĐANG XỬ LÝ: [ {f_type.upper()} ] - [ {d_type.upper()} ]")
            print(f"=======================================================")

            # Chuyển đổi loại chuyến bay (Flights) nếu cần
            if sc["f_curr"] != sc["f_target"]:
                click_nice_select(page, sc["f_curr"], sc["f_target"])

            # Chuyển đổi chiều bay (Departures / Arrivals) nếu cần
            if sc["d_curr"] != sc["d_target"]:
                click_nice_select(page, sc["d_curr"], sc["d_target"])

            # Bấm 'Load more' nhiều lần để tải toàn bộ danh sách trong ngày
            for _ in range(15):
                try:
                    load_more = page.locator("a:has-text('Load more'), button:has-text('Load more')").first
                    if load_more.is_visible():
                        load_more.click(timeout=1500)
                        time.sleep(1)
                    else:
                        break
                except:
                    break

            # Đọc tiêu đề các cột của bảng hiện tại
            headers = [th.inner_text().strip().lower() for th in page.locator("table thead tr th").all()]
            rows = page.locator("table tbody tr").all()
            print(f"Cột bảng thực tế: {headers}")
            print(f"Tổng số dòng cào được: {len(rows)}")

            section_count = 0
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

                # Nhận diện chính xác chuyến bay của Vietnam Airlines
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
                    # Lấy tuyến bay: Nếu Bay Đi lấy Destination, nếu Bay Đến lấy From
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
                        "flight_type": f_type,       # Domestic / International
                        "direction": d_type,           # Departure / Arrival
                        "airport": "SGN",
                        "fr24_url": f"https://www.flightradar24.com/data/flights/{flight_no}",
                        "updated_at": firestore.SERVER_TIMESTAMP
                    }
                    all_flights.append(flight_item)
                    section_count += 1

            print(f"-> Thu thập được {section_count} chuyến bay Vietnam Airlines trong mục này.")

        browser.close()

    print(f"\n=======================================================")
    print(f" TỔNG CỘNG THU THẬP: {len(all_flights)} CHUYẾN BAY THẬT SGN")
    print(f"=======================================================")

    if len(all_flights) > 0:
        # Xóa sạch dữ liệu cũ của ngày hôm nay để nạp mới hoàn toàn
        print("Đang làm sạch dữ liệu cũ trong Firebase...")
        docs = db.collection("flights").where("date", "==", today_str).stream()
        for doc in docs:
            doc.reference.delete()

        # Lưu dữ liệu sạch vào Firestore
        batch = db.batch()
        count = 0
        for flight in all_flights:
            # Mã định danh tài liệu độc nhất: Ngày_ChiềuBay_LoạiChuyến_SốHiệu
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
        print("Cảnh báo: Không có chuyến bay nào được lưu.")

if __name__ == "__main__":
    scrape_viags()
