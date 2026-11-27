import json
import os
import re
import time
from datetime import datetime
import pytz
from playwright.sync_api import sync_playwright
import firebase_admin
from firebase_admin import credentials, firestore

def init_firebase():
    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not sa_json:
        raise ValueError("Thiếu biến môi trường FIREBASE_SERVICE_ACCOUNT!")
    cred_dict = json.loads(sa_json)
    cred = credentials.Certificate(cred_dict)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    return firestore.client()

def scrape_viags():
    db = init_firebase()
    tz_vn = pytz.timezone('Asia/Ho_Chi_Minh')
    today_str = datetime.now(tz_vn).strftime('%Y-%m-%d')
    print(f"=== BẮT ĐẦU CHẨN ĐOÁN & CÀO DỮ LIỆU VIAGS TSN: {today_str} ===")

    captured_requests = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900}
        )
        page = context.new_page()

        # Bắt tất cả các yêu cầu mạng AJAX/API
        def log_request(request):
            if "schedule" in request.url.lower() or "flight" in request.url.lower() or "api" in request.url.lower():
                captured_requests.append((request.method, request.url, request.post_data))
                print(f"[NETWORK] {request.method} -> {request.url}")

        page.on("request", log_request)

        url = "https://www.viags.vn/en-US/flight-schedules"
        print(f"Đang mở trang: {url}")
        page.goto(url, wait_until="networkidle", timeout=60000)
        time.sleep(3)

        # 1. IN CẤU TRÚC HTML CỦA KHUNG CHỌN RA MÀN HÌNH ĐỂ XEM RÕ
        print("\n--- BẮT ĐẦU PHÂN TÍCH CẤU TRÚC KHUNG CHỌN ---")
        filter_html = page.evaluate("""() => {
            const h = Array.from(document.querySelectorAll('h1, h2, h3, div')).find(e => e.innerText && e.innerText.includes('FLIGHT INFORMATION'));
            if (h) {
                const parent = h.closest('.container') || h.parentElement;
                return parent ? parent.outerHTML.substring(0, 3000) : 'Không tìm thấy container';
            }
            return 'Không tìm thấy FLIGHT INFORMATION';
        }""")
        print("HTML KHUNG CHỌN:\n", filter_html[:1500])

        # 2. IN TẤT CẢ THẺ SELECT VÀ CÁC CLASS LIÊN QUAN
        select_info = page.evaluate("""() => {
            const selects = Array.from(document.querySelectorAll('select'));
            return selects.map(s => ({
                id: s.id,
                name: s.name,
                className: s.className,
                options: Array.from(s.options).map(o => ({ text: o.text.trim(), val: o.value }))
            }));
        }""")
        print("\nDANH SÁCH THẺ SELECT:", json.dumps(select_info, indent=2, ensure_ascii=False))

        # 3. THỬ TẤT CẢ CÁC CÁCH KÍCH HOẠT SÀI GÒN (SGN)
        print("\n--- ĐANG THỬ CHỌN HO CHI MINH / SGN ---")
        page.evaluate("""() => {
            // Cách 1: Select2
            if (window.jQuery && window.jQuery.fn && window.jQuery.fn.select2) {
                console.log('Phát hiện Select2');
                window.jQuery('select').val('SGN').trigger('change');
            }
            // Cách 2: Kích hoạt tất cả select chứa Ho Chi Minh hoặc SGN
            document.querySelectorAll('select').forEach(sel => {
                for (let opt of sel.options) {
                    if (opt.text.includes('Ho Chi Minh') || opt.value === 'SGN') {
                        sel.value = opt.value;
                        if (window.jQuery) {
                            window.jQuery(sel).val(opt.value).trigger('change');
                        }
                        sel.dispatchEvent(new Event('change', { bubbles: true }));
                        sel.dispatchEvent(new Event('input', { bubbles: true }));
                        if (sel.onchange) sel.onchange();
                    }
                }
            });
        }""")
        time.sleep(4)

        # Kiểm tra lại số hàng và nội dung bảng sau khi thử kích hoạt
        rows = page.locator("table tbody tr").all()
        print(f"\nSố hàng sau khi kích hoạt SGN: {len(rows)}")
        for idx in range(min(4, len(rows))):
            cols = [td.inner_text().strip() for td in rows[idx].locator("td").all()]
            print(f"  Dòng {idx+1}: {cols}")

        browser.close()

if __name__ == "__main__":
    scrape_viags()
