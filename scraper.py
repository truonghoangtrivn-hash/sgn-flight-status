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

# Hàm chờ ASP.NET UpdatePanel tải xong ngầm 100%
def wait_for_aspnet(page):
    try:
        page.wait_for_function("""() => {
            return window.Sys && Sys.WebForms && Sys.WebForms.PageRequestManager && 
                   !Sys.WebForms.PageRequestManager.getInstance().get_isInAsyncPostBack();
        }""", timeout=15000)
    except:
        pass
    time.sleep(2)

# Hàm đổi thông số VIAGS qua lệnh gốc ASP.NET
def set_viags_filter(page, station_val, trip_val):
    page.evaluate(f"""() => {{
        // Đặt sân bay SGN (Hồ Chí Minh)
        const ddlOrigin = document.querySelector('[name$="ddlOriginal"]');
        if (ddlOrigin) ddlOrigin.value = 'SGN';

        // Đặt loại chuyến (DOM / INT)
        const ddlStation = document.querySelector('[name$="ddlStation"]');
        if (ddlStation) ddlStation.value = '{station_val}';

        // Đặt chiều bay (D: Đi / A: Đến)
        const ddlTrip = document.querySelector('[name$="ddlTrip"]');
        if (ddlTrip) ddlTrip.value = '{trip_val}';

        // Đặt khung giờ (All day: 0000-2359)
        const ddlSched = document.querySelector('[name$="ddlScheduled"]');
        if (ddlSched) ddlSched.value = '0000-2359';

        // Kích hoạt lệnh gốc của VIAGS
        if (typeof __doPostBack === 'function') {{
            __doPostBack(ddlTrip ? ddlTrip.name : 'ctl00$mainContent$ctl00$ddlTrip', '');
        }}
    }}""")
    wait_for_aspnet(page)

# 2. Quy trình cào dữ liệu
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

        # 4 Tổ hợp cần lấy:
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

            # Kích hoạt bộ lọc bằng thông số F12 chính xác
            set_viags_filter(page, task["station"], task["trip"])

            # BẤM NÚT VÀNG 'LOAD MORE' LIÊN TỤC CHO ĐẾN KHI HẾT SẠCH
            load_count = 0
            while True:
                load_more = page.locator("a:has-text('Load more'), button:has-text('Load more'), .btn-load-more, .btn:has-text('Load more')").first
                if load_more.is_visible():
                    load_count += 1
                    print(f"-> Bấm 'Load more' lần {load_count}...")
                    load_more.scroll_into_view_if_needed()
                    load_more.click()
                    wait_for_aspnet(page)
                    if load_count >= 25:
                        break
                else:
                    print(f"-> Đã tải xong toàn bộ danh sách (đã bấm {load_count} lần 'Load more').")
                    break

            # Đọc tiêu đề các cột
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
                    # Lấy tuyến bay: Destination nếu Bay Đi, From nếu Bay Đến
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
        # Xóa sạch dữ liệu cũ ngày hôm nay trong Firebase để tránh xung đột
        print("Đang dọn dẹp dữ liệu cũ trong Firebase...")
        docs = db.collection("flights").where("date", "==", today_str).stream()
        for doc in docs:
            doc.reference.delete()

        # Lưu dữ liệu mới chuẩn xác vào Firestore
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
