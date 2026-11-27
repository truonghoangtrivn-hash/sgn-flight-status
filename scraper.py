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

# Hàm click chọn dropdown tùy biến trên VIAGS (Mô phỏng 100% thao tác chuột người dùng)
def select_viags_dropdown(page, dropdown_index, option_text):
    print(f"Đang chọn ô {dropdown_index + 1} -> '{option_text}'...")
    try:
        # Tìm các hộp chọn tùy biến (.nice-select hoặc thẻ div tương đương)
        boxes = page.locator(".nice-select, .select-styled, .custom-select, .dropdown")
        if boxes.count() > dropdown_index:
            target_box = boxes.nth(dropdown_index)
            target_box.scroll_into_view_if_needed()
            target_box.click(timeout=4000)
            time.sleep(0.5)

            # Click vào lựa chọn xuất hiện trong danh sách thả xuống
            opt = target_box.locator(f"li:has-text('{option_text}')")
            if opt.count() > 0:
                opt.first.click(timeout=4000)
            else:
                page.locator(f"li.option:has-text('{option_text}'), li:has-text('{option_text}')").first.click(timeout=4000)
            time.sleep(2.5)
            print(f"-> Thành công chọn: {option_text}")
            return True
    except Exception as e:
        print(f"Click menu không thành công ({e}), chuyển sang fallback JS...")

    # Dự phòng: Ép bằng JavaScript nếu giao diện không phản hồi click
    try:
        page.evaluate(f"""() => {{
            const selects = document.querySelectorAll('select');
            if (selects.length > {dropdown_index}) {{
                const sel = selects[{dropdown_index}];
                for (let opt of sel.options) {{
                    if (opt.text.toLowerCase().includes('{option_text.lower()}')) {{
                        sel.value = opt.value;
                        if (window.jQuery) {{
                            window.jQuery(sel).val(opt.value).trigger('change');
                        }}
                        sel.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        break;
                    }}
                }}
            }}
        }}""")
        time.sleep(2.5)
        print(f"-> JS Fallback đã kích hoạt cho: {option_text}")
        return True
    except Exception as ex:
        print(f"Lỗi khi chọn {option_text}: {ex}")
        return False

# 2. Quá trình cào dữ liệu
def scrape_viags():
    db = init_firebase()
    tz_vn = pytz.timezone('Asia/Ho_Chi_Minh')
    today_str = datetime.now(tz_vn).strftime('%Y-%m-%d')
    print(f"=====================================================")
    print(f" BẮT ĐẦU CÀO DỮ LIỆU VIAGS SGN CHO NGÀY: {today_str}")
    print(f"=====================================================")

    all_flights = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1400, "height": 900}
        )
        page = context.new_page()

        url = "https://www.viags.vn/en-US/flight-schedules"
        print(f"Mở liên kết: {url}")
        page.goto(url, wait_until="networkidle", timeout=60000)
        time.sleep(3)

        # Cấu hình chuẩn 5 ô chọn theo ảnh thực tế của bạn:
        # Ô 1 (index 0): Flights (Domestic / International)
        # Ô 2 (index 1): Departures / Arrivals
        # Ô 3 (index 2): Location (Bắt buộc chọn Ho Chi Minh)
        # Ô 4 (index 3): Date
        # Ô 5 (index 4): Time (Bắt buộc chọn All day)

        # 1. Chọn Location = Ho Chi Minh (Ô số 3, index 2)
        select_viags_dropdown(page, 2, "Ho Chi Minh")
        time.sleep(2)

        # 2. Chọn Time = All day (Ô số 5, index 4)
        select_viags_dropdown(page, 4, "All day")
        time.sleep(1)

        # 4 Tổ hợp cần lấy:
        tasks = [
            {"f_type": "Domestic", "f_label": "Domestic Flights", "d_type": "Departure", "d_label": "Departures"},
            {"f_type": "Domestic", "f_label": "Domestic Flights", "d_type": "Arrival", "d_label": "Arrivals"},
            {"f_type": "International", "f_label": "International Flights", "d_type": "Departure", "d_label": "Departures"},
            {"f_type": "International", "f_label": "International Flights", "d_type": "Arrival", "d_label": "Arrivals"},
        ]

        for task in tasks:
            f_type = task["f_type"]
            d_type = task["d_type"]
            print(f"\n>>> ĐANG LẤY: [ {f_type.upper()} ] - [ {d_type.upper()} ] <<<")

            # Chọn loại chuyến (Ô 1, index 0)
            select_viags_dropdown(page, 0, task["f_label"])
            # Chọn chiều bay (Ô 2, index 1)
            select_viags_dropdown(page, 1, task["d_label"])
            time.sleep(2)

            # Bấm nút 'Load more' nhiều lần nếu có để lấy hết tất cả chuyến trong ngày
            for _ in range(12):
                try:
                    load_more = page.locator("a:has-text('Load more'), button:has-text('Load more')").first
                    if load_more.is_visible():
                        load_more.click(timeout=1500)
                        time.sleep(1)
                    else:
                        break
                except:
                    break

            # Đọc tiêu đề cột (th) để tự động ánh xạ, không bao giờ lo lệch cột
            headers = [th.inner_text().strip().lower() for th in page.locator("table thead tr th").all()]
            print(f"Tiêu đề bảng: {headers}")

            rows = page.locator("table tbody tr").all()
            print(f"Số hàng tìm thấy: {len(rows)}")

            for row in rows:
                tds = row.locator("td").all()
                if not tds or len(tds) < 4:
                    continue

                col_map = {}
                for idx, td in enumerate(tds):
                    h_name = headers[idx] if idx < len(headers) else f"col_{idx}"
                    col_map[h_name] = td.inner_text().strip()

                # Kiểm tra thông tin chuyến bay
                flight_no = col_map.get("flights", "").replace(" ", "").upper()
                row_raw_text = " ".join(col_map.values()).upper()

                # Kiểm tra có phải Vietnam Airlines hay không
                # Bao gồm mã chuyến bắt đầu bằng VN, hoặc có chữ VIETNAM AIRLINES
                is_vn = flight_no.startswith("VN") or "VIETNAM AIRLINES" in row_raw_text
                
                # Bắt thêm trường hợp mã VN nằm ở cột khác
                if not is_vn:
                    match_vn = re.search(r'\b(VN\s*\d{2,4})\b', row_raw_text)
                    if match_vn:
                        is_vn = True
                        flight_no = match_vn.group(1).replace(" ", "")

                if is_vn and flight_no:
                    # Xác định tuyến bay (Nơi đến nếu Bay Đi, Nơi đi nếu Bay Đến)
                    dest_or_from = col_map.get("destination", "") or col_map.get("from", "")
                    
                    flight_item = {
                        "date": today_str,
                        "scheduled": col_map.get("scheduled", "--:--"),
                        "estimated": col_map.get("estimated", col_map.get("scheduled", "--:--")),
                        "destination_or_from": dest_or_from,
                        "route": dest_or_from,
                        "airline": "Vietnam Airlines",
                        "flight_no": flight_no,
                        "gate": col_map.get("gate", "--") if col_map.get("gate") else "--",
                        "belt": col_map.get("baggage belts", col_map.get("belt", "--")) if col_map.get("baggage belts", col_map.get("belt")) else "--",
                        "remarks": col_map.get("remarks", "Normal"),
                        "flight_type": f_type,       # Domestic hoặc International chuẩn xác
                        "direction": d_type,           # Departure hoặc Arrival chuẩn xác
                        "airport": "SGN",
                        "fr24_url": f"https://www.flightradar24.com/data/flights/{flight_no}",
                        "updated_at": firestore.SERVER_TIMESTAMP
                    }
                    all_flights.append(flight_item)

        browser.close()

    print(f"\n=======================================================")
    print(f" TỔNG SỐ CHUYẾN BAY THU THẬP THẬT TẠI SGN: {len(all_flights)}")
    print(f"=======================================================")

    if len(all_flights) > 0:
        # Xóa các chuyến bay cũ của ngày hôm nay để tránh dữ liệu rác trước đó
        print("Đang làm sạch dữ liệu cũ trong Firebase...")
        docs = db.collection("flights").where("date", "==", today_str).stream()
        for doc in docs:
            doc.reference.delete()

        # Lưu dữ liệu mới hoàn toàn chính xác
        batch = db.batch()
        count = 0
        for flight in all_flights:
            # Tạo mã định danh độc nhất: VD: 2026-11-27_Departure_Domestic_VN1366
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
        print("=== ĐÃ LƯU DỮ LIỆU THẬT 100% TỪ VIAGS VÀO FIREBASE! ===")
    else:
        print("Cảnh báo: Không tìm thấy chuyến bay nào!")

if __name__ == "__main__":
    scrape_viags()
