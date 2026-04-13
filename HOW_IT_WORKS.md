# Cách hoạt động của Google Ads Transparency Scraper

## Mục tiêu

Cào dữ liệu từ trang **Google Ads Transparency Center** của một nhà quảng cáo cụ thể, lấy:
- **Tên sản phẩm** (headline/mô tả của quảng cáo)
- **Landing Page** (URL đích mà quảng cáo dẫn đến)
- **Link YouTube** (nếu là video ad)

---

## Tổng quan kiến trúc

Trang Ads Transparency dùng **Angular web components** và render toàn bộ nội dung bằng JavaScript, nên không thể dùng `requests` + `BeautifulSoup` thông thường. Script dùng **Playwright** (headless Chromium) để điều khiển trình duyệt thật.

---

## Các bước hoạt động

### Bước 1 — Thu thập danh sách link quảng cáo

```
Main page (advertiser page)
└── Scroll xuống để load hết ads (infinite scroll)
└── Lấy href từ mỗi thẻ <creative-preview a[href]>
    Ví dụ: /advertiser/AR.../creative/CR05973...?region=VN
```

- Mỗi card quảng cáo có dạng custom element `<creative-preview>`.
- Script scroll liên tục đến cuối trang, dừng khi không còn card mới xuất hiện.
- Kết quả: danh sách URL chi tiết của từng creative.

---

### Bước 2 — Mở từng trang chi tiết quảng cáo

Với mỗi creative URL (ví dụ `/advertiser/.../creative/CR123`):

```
Creative Detail Page
├── Frame chính (Angular app)
├── tpc.googlesyndication.com/safeframe/...    ← wrapper
│   └── tpc.googlesyndication.com/pagead/gadgets/discover_video_ads/...
│       ├── [TEXT] Tên sản phẩm + mô tả       ← lấy từ đây
│       ├── [LINK] Landing page URL            ← lấy từ đây
│       └── <iframe> youtube.com/embed/VIDEO_ID  ← lấy từ đây
└── ...các frame khác (sidebar cards)
```

---

### Bước 3 — Lấy Tên sản phẩm & Landing Page

Google render nội dung quảng cáo trong một iframe tên là **`discover_video_ads`** (hoặc `youtube_vertical_player_media` cho vertical video).

Script lắng nghe sự kiện `framenavigated` và bắt **frame đầu tiên** có URL chứa `discover_video_ads` — đây là preview của creative đang được xem (không phải sidebar).

Từ frame đó:
- Gọi `frame.inner_text("body")` → lấy toàn bộ text, lọc bỏ "Sponsored", timestamp → **Tên sản phẩm**
- Gọi `frame.query_selector_all("a[href]")` → lấy link đầu tiên không phải domain Google → **Landing Page**

---

### Bước 4 — Lấy Link YouTube

Nếu quảng cáo là video ad, Chromium sẽ load một iframe embed YouTube:

```
youtube.com/embed/VIDEO_ID?rel=0&version=3&...
```

Script bắt sự kiện `framenavigated`, tìm frame có URL dạng `youtube.com/embed/{11 ký tự}` và extract Video ID.

Kết quả: `https://www.youtube.com/watch?v=VIDEO_ID`

> Nếu là **image ad** (không có YouTube), trường này để trống.

---

### Bước 5 — Xuất Excel

Dùng `pandas` + `openpyxl` để tạo file `ads_export.xlsx` với các cột:

| STT | Creative ID | Tên sản phẩm | Landing Page | Link YouTube |
|-----|-------------|--------------|--------------|--------------|
| 1   | CR05973...  | Ưu đãi 40%... | https://... | https://youtube.com/... |

Các ô chứa URL được format thành **hyperlink** có thể click.

---

## Stack kỹ thuật

| Thư viện | Vai trò |
|----------|---------|
| `playwright` | Điều khiển Chromium, intercept frames |
| `pandas` | Tạo DataFrame và xuất Excel |
| `openpyxl` | Format file .xlsx (hyperlink, column width) |
| `asyncio` | Chạy async để tối ưu tốc độ |

---

## Lý do KHÔNG dùng Google Ads API trực tiếp

Trang Ads Transparency **không có public API chính thức** cho việc đọc creative content. Các endpoint nội bộ (`/anji/_/rpc/SearchService/SearchCreatives`) trả về metadata của creative nhưng **không chứa**:
- Text mô tả sản phẩm
- URL đích (landing page)

Nội dung thực sự được load qua `displayads-formats.googleusercontent.com/ads/preview/content.js` — một file JS được obfuscate (mã hóa), render trong iframe. Cách duy nhất đọc được là để trình duyệt thật render và đọc DOM của iframe đó.
