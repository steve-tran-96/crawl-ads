"""
scraper.py — Core scraping logic (dùng standalone hoặc import từ main.py)
"""

import asyncio
import logging
import re
import sys
from pathlib import Path
from typing import Callable, Awaitable

import pandas as pd
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scraper")

SCROLL_PAUSE      = 1.0   # buffer sau networkidle
MAX_EMPTY_SCROLLS = 6
PAGE_WAIT         = 8
SCROLL_STEP       = 600   # nhỏ hơn để không bỏ qua trigger point lazy load

BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
    "--lang=vi-VN",
]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def yt_id_from_embed_url(url: str) -> str | None:
    m = re.search(r"youtube\.com/embed/([A-Za-z0-9_\-]{11})", url)
    return m.group(1) if m else None


def yt_link(video_id: str | None) -> str:
    return f"https://www.youtube.com/watch?v={video_id}" if video_id else ""


async def scroll_and_collect_links(page, cancelled=None) -> list[str]:
    seen: set[str] = set()
    no_change = 0
    scroll_y = 0
    prev_scroll_height = 0

    while no_change < MAX_EMPTY_SCROLLS:
        scroll_y += SCROLL_STEP
        await page.evaluate(f"window.scrollTo(0, {scroll_y})")

        # Chờ network load xong batch mới (quan trọng hơn sleep cố định)
        try:
            await page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass
        await asyncio.sleep(SCROLL_PAUSE)

        cards = await page.query_selector_all("creative-preview a[href]")
        hrefs = set()
        for c in cards:
            href = await c.get_attribute("href")
            if href and "/creative/" in href:
                hrefs.add(href)
        new = hrefs - seen
        if new:
            seen.update(new)
            no_change = 0
            log.info(f"Scroll y={scroll_y}: tìm thấy {len(seen)} (+{len(new)} mới)")
        else:
            no_change += 1
            log.info(f"Scroll y={scroll_y}: không mới ({no_change}/{MAX_EMPTY_SCROLLS})")

        if cancelled and cancelled():
            log.info("Huỷ trong lúc scroll.")
            break

        scroll_height = await page.evaluate("document.body.scrollHeight")
        if scroll_height == prev_scroll_height and no_change >= 3:
            log.info(f"Trang ngừng load thêm. Tổng: {len(seen)} quảng cáo.")
            break
        prev_scroll_height = scroll_height

    log.info(f"Kết thúc scroll. Tổng: {len(seen)} quảng cáo.")
    return sorted(seen)


async def scrape_creative(browser, relative_href: str) -> dict:
    result = {"creative_id": "", "product_name": "", "landing_page": "", "youtube_link": ""}

    m = re.search(r"creative/(CR[A-Za-z0-9]+)", relative_href)
    if m:
        result["creative_id"] = m.group(1)

    detail_url = f"https://adstransparency.google.com{relative_href}"

    ctx = await browser.new_context(
        locale="vi-VN",
        user_agent=USER_AGENT,
        viewport={"width": 1440, "height": 900},
    )
    page = await ctx.new_page()

    yt_ids: list[str] = []
    first_dv_frame = None

    def on_frame(frame):
        nonlocal first_dv_frame
        url = frame.url
        if "youtube.com/embed/" in url:
            vid = yt_id_from_embed_url(url)
            if vid and vid not in yt_ids:
                yt_ids.append(vid)
        if "discover_video_ads" in url and first_dv_frame is None:
            first_dv_frame = frame

    page.on("framenavigated", on_frame)

    try:
        await page.goto(detail_url, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(PAGE_WAIT)

        if yt_ids:
            result["youtube_link"] = yt_link(yt_ids[0])

        if first_dv_frame is not None:
            try:
                body_text = await first_dv_frame.inner_text("body")
                lines = [
                    ln.strip() for ln in body_text.splitlines()
                    if ln.strip()
                    and ln.strip() not in ("Sponsored", "Được tài trợ", "00:00")
                    and not re.fullmatch(r"\d+:\d+", ln.strip())
                ]
                result["product_name"] = " ".join(lines).strip()

                anchors = await first_dv_frame.query_selector_all("a[href]")
                for a in anchors:
                    href = await a.get_attribute("href") or ""
                    if (
                        href.startswith("http")
                        and "google" not in href
                        and "ytimg" not in href
                        and "gstatic" not in href
                        and "googleapis" not in href
                        and "googlesyndication" not in href
                        and "ampproject" not in href
                    ):
                        result["landing_page"] = href
                        break
            except Exception:
                pass
    except Exception as e:
        result["error"] = str(e)
    finally:
        await ctx.close()

    return result


async def run_scrape(
    advertiser_url: str,
    output_file: Path,
    on_progress: Callable[[int, int, str], Awaitable[None]] | None = None,
    on_status: Callable[[str], Awaitable[None]] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Path:
    """
    Main entry point dùng từ API hoặc CLI.
    on_progress(current, total, creative_id) được gọi sau mỗi ad.
    on_status(message) được gọi để cập nhật trạng thái chi tiết.
    Trả về path của file Excel.
    """
    def cancelled() -> bool:
        return should_cancel is not None and should_cancel()

    async def status(msg: str):
        log.info(msg)
        if on_status:
            await on_status(msg)

    async with async_playwright() as pw:
        await status("Đang khởi động trình duyệt...")
        browser = await pw.chromium.launch(headless=True, args=BROWSER_ARGS)

        # Bước 1: Thu thập link
        await status("Đang mở trang advertiser...")
        ctx0 = await browser.new_context(
            locale="vi-VN", user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
        )
        page0 = await ctx0.new_page()
        await page0.goto(advertiser_url, wait_until="domcontentloaded", timeout=60_000)
        await status("Trang đã load, đang chờ quảng cáo xuất hiện...")

        try:
            await page0.wait_for_selector("creative-preview", timeout=30_000)
        except PlaywrightTimeout:
            page_title = await page0.title()
            page_url = page0.url
            body_text = (await page0.inner_text("body"))[:500]
            log.error(f"Timeout! Title: '{page_title}' | URL: {page_url}")
            log.error(f"Nội dung trang (500 ký tự đầu): {body_text}")
            await browser.close()
            raise RuntimeError(
                f"Không tìm thấy quảng cáo nào. "
                f"Title: '{page_title}' | "
                f"Nội dung: {body_text[:200]}"
            )

        await status("Đang scroll thu thập danh sách quảng cáo...")
        hrefs = await scroll_and_collect_links(page0, cancelled)
        await ctx0.close()

        total = len(hrefs)
        await status(f"Tìm thấy {total} quảng cáo. Bắt đầu scrape chi tiết...")

        # Bước 2: Scrape từng creative
        rows = []
        for i, href in enumerate(hrefs, 1):
            if cancelled():
                log.info(f"Huỷ tại creative {i}/{total}.")
                break

            cid = re.search(r"creative/(CR[A-Za-z0-9]+)", href)
            cid_str = cid.group(1) if cid else "?"

            if on_progress:
                await on_progress(i - 1, total, cid_str)

            log.info(f"[{i}/{total}] Scraping {cid_str}...")
            data = await scrape_creative(browser, href)
            rows.append(data)

            if on_progress:
                await on_progress(i, total, cid_str)

        await browser.close()

    # Bước 3: Xuất Excel
    df = pd.DataFrame(rows)
    df.index = df.index + 1
    df.rename(columns={
        "creative_id": "Creative ID",
        "product_name": "Tên sản phẩm",
        "landing_page": "Landing Page",
        "youtube_link": "Link YouTube",
    }, inplace=True)

    output_file.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Ads", index=True, index_label="STT")
        ws = writer.sheets["Ads"]
        for col_letter, width in {"A": 6, "B": 22, "C": 55, "D": 55, "E": 45}.items():
            ws.column_dimensions[col_letter].width = width
        for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
            for cell in row:
                if cell.value and str(cell.value).startswith("http"):
                    cell.hyperlink = cell.value
                    cell.style = "Hyperlink"

    return output_file


# ── Chạy standalone ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    url = (
        "https://adstransparency.google.com/advertiser/"
        "AR00718079396149198849?region=VN&preset-date=Today"
    )
    out = Path(__file__).parent / "ads_export.xlsx"

    async def _cli():
        async def progress(cur, total, cid):
            print(f"  [{cur:>3}/{total}] {cid}…", end="\r")
        result = await run_scrape(url, out, progress)
        print(f"\n✓ Done → {result}")

    asyncio.run(_cli())
