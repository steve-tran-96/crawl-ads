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

SCROLL_PAUSE      = 2.0
MAX_EMPTY_SCROLLS = 6
PAGE_WAIT         = 8

BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
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


async def scroll_and_collect_links(page) -> list[str]:
    seen: set[str] = set()
    no_change = 0
    while no_change < MAX_EMPTY_SCROLLS:
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
        else:
            no_change += 1
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(SCROLL_PAUSE)
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
) -> Path:
    """
    Main entry point dùng từ API hoặc CLI.
    on_progress(current, total, creative_id) được gọi sau mỗi ad.
    on_status(message) được gọi để cập nhật trạng thái chi tiết.
    Trả về path của file Excel.
    """
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
            await browser.close()
            raise RuntimeError("Không tìm thấy quảng cáo nào. Kiểm tra lại URL.")

        await status("Đang scroll thu thập danh sách quảng cáo...")
        hrefs = await scroll_and_collect_links(page0)
        await ctx0.close()

        total = len(hrefs)
        await status(f"Tìm thấy {total} quảng cáo. Bắt đầu scrape chi tiết...")

        # Bước 2: Scrape từng creative
        rows = []
        for i, href in enumerate(hrefs, 1):
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
