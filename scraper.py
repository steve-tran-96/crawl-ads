"""
scraper.py — Core scraping logic (dùng standalone hoặc import từ main.py)
"""

import asyncio
from dataclasses import dataclass
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Callable, Awaitable
from urllib.parse import parse_qs, urlencode, urlsplit

import pandas as pd
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scraper")

SCROLL_PAUSE       = 0.35  # buffer sau networkidle
MAX_EMPTY_SCROLLS  = 12
MAX_STALL_SECONDS  = 12
PER_SCROLL_TIMEOUT = 3
PAGE_WAIT          = 8
SCROLL_STEP        = 600   # nhỏ hơn để không bỏ qua trigger point lazy load
BOTTOM_EPSILON     = 48

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

EXPORT_COLUMNS = ["creative_id", "product_name", "landing_page", "youtube_link"]


@dataclass
class ScrapeResult:
    output_file: Path
    scanned_total: int
    exported_total: int


def yt_id_from_embed_url(url: str) -> str | None:
    m = re.search(r"youtube\.com/embed/([A-Za-z0-9_\-]{11})", url)
    return m.group(1) if m else None


def yt_link(video_id: str | None) -> str:
    return f"https://www.youtube.com/watch?v={video_id}" if video_id else ""


def creative_href_from_advertiser_url(
    advertiser_url: str,
    advertiser_id: str,
    creative_id: str,
) -> str:
    parsed = urlsplit(advertiser_url)
    query = f"?{parsed.query}" if parsed.query else ""
    return f"/advertiser/{advertiser_id}/creative/{creative_id}{query}"


def decode_search_creatives_response(text: str) -> dict:
    payload = text.strip()
    if payload.startswith(")]}'"):
        newline = payload.find("\n")
        if newline != -1:
            payload = payload[newline + 1 :].lstrip()
    return json.loads(payload)


def extract_creative_hrefs_from_rpc(payload: dict, advertiser_url: str) -> list[str]:
    hrefs: list[str] = []
    for item in payload.get("1", []):
        if not isinstance(item, dict):
            continue
        advertiser_id = item.get("1")
        creative_id = item.get("2")
        if not advertiser_id or not creative_id:
            continue
        hrefs.append(
            creative_href_from_advertiser_url(
                advertiser_url=advertiser_url,
                advertiser_id=advertiser_id,
                creative_id=creative_id,
            )
        )
    return hrefs


def extract_next_cursor_from_rpc(payload: dict) -> str | None:
    # SearchCreatives trả cursor trang sau ở field "2".
    # Các field "4"/"5" thường là offset/total dạng số ("700", "800"),
    # không phải opaque page token để replay request tiếp theo.
    for key in ("2", "4"):
        cursor = payload.get(key)
        if isinstance(cursor, str) and cursor and not cursor.isdigit():
            return cursor
    return None


def parse_form_body(post_data: str | None) -> dict[str, str]:
    if not post_data:
        return {}
    parsed = parse_qs(post_data, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def build_rpc_replay_headers(headers: dict[str, str]) -> dict[str, str]:
    replay = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered in {"content-length", "host", "cookie"}:
            continue
        replay[key] = value

    replay.setdefault("content-type", "application/x-www-form-urlencoded;charset=UTF-8")
    return replay


async def fetch_search_creatives_page(
    page,
    request_url: str,
    form_fields: dict[str, str],
    request_headers: dict[str, str],
) -> str:
    response = await page.context.request.post(
        request_url,
        headers=build_rpc_replay_headers(request_headers),
        data=urlencode(form_fields),
        fail_on_status_code=False,
        timeout=30_000,
    )
    try:
        text = await response.text()
    finally:
        await response.dispose()

    if not response.ok:
        raise RuntimeError(
            f"SearchCreatives RPC lỗi HTTP {response.status} khi phân trang."
        )
    return text


async def collect_links_via_rpc(
    page,
    advertiser_url: str,
    initial_response,
    initial_payload: dict | None = None,
    cancelled=None,
) -> list[str]:
    seen: set[str] = set()
    first_payload = initial_payload or decode_search_creatives_response(await initial_response.text())
    first_hrefs = extract_creative_hrefs_from_rpc(first_payload, advertiser_url)
    if first_hrefs:
        seen.update(first_hrefs)
        log.info("RPC SearchCreatives #1: tổng %s quảng cáo (+%s mới)", len(seen), len(first_hrefs))

    form_fields = parse_form_body(initial_response.request.post_data)
    if "f.req" not in form_fields:
        raise RuntimeError("Không đọc được f.req từ SearchCreatives request đầu tiên.")
    request_headers = await initial_response.request.all_headers()

    request_payload = json.loads(form_fields["f.req"])
    if not isinstance(request_payload, dict):
        raise RuntimeError("f.req của SearchCreatives không đúng định dạng mong đợi.")

    next_cursor = extract_next_cursor_from_rpc(first_payload)
    seen_cursors: set[str] = set()
    page_index = 1

    while next_cursor:
        if cancelled and cancelled():
            log.info("Huỷ trong lúc phân trang RPC.")
            break

        if next_cursor in seen_cursors:
            log.info("Cursor SearchCreatives lặp lại, dừng tại %s quảng cáo.", len(seen))
            break
        seen_cursors.add(next_cursor)

        request_payload["4"] = next_cursor
        form_fields["f.req"] = json.dumps(request_payload, separators=(",", ":"))

        page_index += 1
        response_text = await fetch_search_creatives_page(
            page=page,
            request_url=initial_response.url,
            form_fields=form_fields,
            request_headers=request_headers,
        )
        payload = decode_search_creatives_response(response_text)
        hrefs = extract_creative_hrefs_from_rpc(payload, advertiser_url)
        new = [href for href in hrefs if href not in seen]
        if new:
            seen.update(new)
        log.info(
            "RPC SearchCreatives #%s: tổng %s quảng cáo (+%s mới)",
            page_index,
            len(seen),
            len(new),
        )

        next_cursor = extract_next_cursor_from_rpc(payload)
        if not new and not next_cursor:
            break

    log.info("Kết thúc phân trang RPC. Tổng: %s quảng cáo.", len(seen))
    return sorted(seen)


async def scroll_and_collect_links(
    page,
    advertiser_url: str,
    cancelled=None,
    initial_hrefs: list[str] | None = None,
) -> list[str]:
    seen: set[str] = set()
    no_change = 0
    last_growth_at = time.monotonic()
    bottom_hits = 0
    rpc_count = 0
    pending_response_tasks: set[asyncio.Task] = set()
    response_lock = asyncio.Lock()
    rpc_event = asyncio.Event()

    async def add_hrefs(hrefs: list[str], source: str) -> int:
        nonlocal last_growth_at
        new = [href for href in hrefs if href not in seen]
        if not new:
            return 0
        seen.update(new)
        last_growth_at = time.monotonic()
        rpc_event.set()
        log.info("%s: tổng %s quảng cáo (+%s mới)", source, len(seen), len(new))
        return len(new)

    async def collect_visible_hrefs() -> set[str]:
        cards = await page.query_selector_all("creative-preview a[href]")
        hrefs = set()
        for c in cards:
            href = await c.get_attribute("href")
            if href and "/creative/" in href:
                hrefs.add(href)
        return hrefs

    async def get_scroll_metrics() -> dict:
        return await page.evaluate(
            """
            () => {
              const creatives = Array.from(document.querySelectorAll('creative-preview'));
              const isScrollable = (el) => {
                if (!(el instanceof HTMLElement)) return false;
                const style = getComputedStyle(el);
                const overflowScrollable =
                  /(auto|scroll|overlay)/.test(style.overflowY) ||
                  el === document.scrollingElement;
                return overflowScrollable && el.scrollHeight - el.clientHeight > 120;
              };

              const pickAncestor = (node) => {
                let cur = node instanceof HTMLElement ? node.parentElement : null;
                while (cur) {
                  if (isScrollable(cur)) return cur;
                  cur = cur.parentElement;
                }
                return document.scrollingElement || document.documentElement;
              };

              const counts = new Map();
              for (const creative of creatives) {
                const owner = pickAncestor(creative);
                counts.set(owner, (counts.get(owner) || 0) + 1);
              }

              let best = document.scrollingElement || document.documentElement;
              let bestCount = -1;
              for (const [el, count] of counts.entries()) {
                if (count > bestCount) {
                  best = el;
                  bestCount = count;
                }
              }

              if (!best) {
                best = document.scrollingElement || document.documentElement;
              }

              const useWindow =
                best === document.body ||
                best === document.documentElement ||
                best === document.scrollingElement;

              return {
                useWindow,
                tagName: best.tagName || 'DOCUMENT',
                className: useWindow ? '' : (best.className || ''),
                left: useWindow ? 0 : best.getBoundingClientRect().left,
                top: useWindow ? 0 : best.getBoundingClientRect().top,
                width: useWindow ? window.innerWidth : best.getBoundingClientRect().width,
                height: useWindow ? window.innerHeight : best.getBoundingClientRect().height,
                scrollTop: useWindow ? window.scrollY : best.scrollTop,
                clientHeight: useWindow ? window.innerHeight : best.clientHeight,
                scrollHeight: best.scrollHeight,
              };
            }
            """
        )

    async def scroll_forward(step: int) -> dict:
        metrics_before = await get_scroll_metrics()
        target_x = max(40, min(metrics_before["left"] + (metrics_before["width"] / 2), 1400))
        target_y = max(
            80,
            min(metrics_before["top"] + min(metrics_before["height"] * 0.8, metrics_before["height"] - 24), 860),
        )

        await page.mouse.move(target_x, target_y)
        await page.mouse.wheel(0, step)
        await asyncio.sleep(0.25)

        metrics_after = await get_scroll_metrics()
        if metrics_after["scrollTop"] == metrics_before["scrollTop"]:
            metrics_after = await page.evaluate(
                """
                (step) => {
                  const creatives = Array.from(document.querySelectorAll('creative-preview'));
                  const isScrollable = (el) => {
                    if (!(el instanceof HTMLElement)) return false;
                    const style = getComputedStyle(el);
                    const overflowScrollable =
                      /(auto|scroll|overlay)/.test(style.overflowY) ||
                      el === document.scrollingElement;
                    return overflowScrollable && el.scrollHeight - el.clientHeight > 120;
                  };

                  const pickAncestor = (node) => {
                    let cur = node instanceof HTMLElement ? node.parentElement : null;
                    while (cur) {
                      if (isScrollable(cur)) return cur;
                      cur = cur.parentElement;
                    }
                    return document.scrollingElement || document.documentElement;
                  };

                  const counts = new Map();
                  for (const creative of creatives) {
                    const owner = pickAncestor(creative);
                    counts.set(owner, (counts.get(owner) || 0) + 1);
                  }

                  let best = document.scrollingElement || document.documentElement;
                  let bestCount = -1;
                  for (const [el, count] of counts.entries()) {
                    if (count > bestCount) {
                      best = el;
                      bestCount = count;
                    }
                  }

                  if (!best) {
                    best = document.scrollingElement || document.documentElement;
                  }

                  const useWindow =
                    best === document.body ||
                    best === document.documentElement ||
                    best === document.scrollingElement;

                  if (useWindow) {
                    window.scrollTo(0, window.scrollY + step);
                  } else {
                    best.scrollTop += step;
                  }

                  return {
                    useWindow,
                    tagName: best.tagName || 'DOCUMENT',
                    className: useWindow ? '' : (best.className || ''),
                    left: useWindow ? 0 : best.getBoundingClientRect().left,
                    top: useWindow ? 0 : best.getBoundingClientRect().top,
                    width: useWindow ? window.innerWidth : best.getBoundingClientRect().width,
                    height: useWindow ? window.innerHeight : best.getBoundingClientRect().height,
                    scrollTop: useWindow ? window.scrollY : best.scrollTop,
                    clientHeight: useWindow ? window.innerHeight : best.clientHeight,
                    scrollHeight: best.scrollHeight,
                  };
                }
                """,
                step,
            )

        return metrics_after

    async def process_search_creatives_response(response) -> None:
        nonlocal rpc_count
        try:
            payload = decode_search_creatives_response(await response.text())
            hrefs = extract_creative_hrefs_from_rpc(payload, advertiser_url)
            async with response_lock:
                rpc_count += 1
                await add_hrefs(hrefs, f"RPC SearchCreatives #{rpc_count}")
        except Exception as exc:
            log.warning("Không parse được SearchCreatives response: %s", exc)

    def on_response(response) -> None:
        if response.request.method != "POST":
            return
        if "SearchService/SearchCreatives" not in response.url:
            return
        task = asyncio.create_task(process_search_creatives_response(response))
        pending_response_tasks.add(task)
        task.add_done_callback(pending_response_tasks.discard)

    page.context.on("response", on_response)

    if initial_hrefs:
        await add_hrefs(sorted(initial_hrefs), "RPC batch ban đầu")

    visible_hrefs = await collect_visible_hrefs()
    if visible_hrefs:
        await add_hrefs(sorted(visible_hrefs), "DOM batch ban đầu")

    try:
        while no_change < MAX_EMPTY_SCROLLS:
            rpc_event.clear()
            metrics_before = await get_scroll_metrics()
            metrics_after_scroll = await scroll_forward(SCROLL_STEP)
            log.info(
                "Wheel target [%s.%s] top %.0f -> %.0f",
                metrics_after_scroll["tagName"],
                metrics_after_scroll["className"] or "-",
                metrics_before["scrollTop"],
                metrics_after_scroll["scrollTop"],
            )

            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            await asyncio.sleep(SCROLL_PAUSE)

            found_new_this_round = False
            wait_deadline = time.monotonic() + PER_SCROLL_TIMEOUT

            while time.monotonic() < wait_deadline:
                if cancelled and cancelled():
                    log.info("Huỷ trong lúc scroll.")
                    break

                if rpc_event.is_set():
                    rpc_event.clear()
                    no_change = 0
                    found_new_this_round = True
                    await asyncio.sleep(0.5)
                    continue

                hrefs = sorted(await collect_visible_hrefs())
                async with response_lock:
                    added_from_dom = await add_hrefs(hrefs, "DOM")
                if added_from_dom:
                    no_change = 0
                    found_new_this_round = True
                    await asyncio.sleep(0.5)
                    continue

                await asyncio.sleep(0.75)

            if cancelled and cancelled():
                log.info("Huỷ trong lúc scroll.")
                break

            if not found_new_this_round:
                no_change += 1
                log.info(
                    "Scroll [%s] top %.0f -> %.0f: chưa có ad mới (%s/%s, im lặng %.1fs)",
                    metrics_after_scroll["tagName"],
                    metrics_before["scrollTop"],
                    metrics_after_scroll["scrollTop"],
                    no_change,
                    MAX_EMPTY_SCROLLS,
                    time.monotonic() - last_growth_at,
                )

            metrics_after_wait = await get_scroll_metrics()
            at_bottom = (
                metrics_after_wait["scrollTop"] + metrics_after_wait["clientHeight"]
                >= metrics_after_wait["scrollHeight"] - BOTTOM_EPSILON
            )
            if at_bottom:
                bottom_hits += 1
            else:
                bottom_hits = 0

            stalled_for = time.monotonic() - last_growth_at
            if bottom_hits >= 2 and stalled_for >= MAX_STALL_SECONDS:
                log.info(
                    "Đã chạm cuối vùng scroll và không có ad mới trong %.1fs. Dừng tại %s ads.",
                    stalled_for,
                    len(seen),
                )
                break
    finally:
        if pending_response_tasks:
            await asyncio.gather(*pending_response_tasks, return_exceptions=True)
        page.context.remove_listener("response", on_response)

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
) -> ScrapeResult:
    """
    Main entry point dùng từ API hoặc CLI.
    on_progress(current, total, creative_id) được gọi sau mỗi ad.
    on_status(message) được gọi để cập nhật trạng thái chi tiết.
    Trả về file Excel cùng thống kê số lượng creative đã quét / được xuất.
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
        initial_rpc_response = None
        try:
            async with page0.expect_response(
                lambda resp: (
                    resp.request.method == "POST"
                    and "SearchService/SearchCreatives" in resp.url
                ),
                timeout=60_000,
            ) as first_search_rpc:
                await page0.goto(advertiser_url, wait_until="domcontentloaded", timeout=60_000)
            initial_rpc_response = await first_search_rpc.value
        except PlaywrightTimeout:
            initial_rpc_response = None
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

        await status("Đang thu thập danh sách quảng cáo từ SearchCreatives...")
        first_rpc_hrefs: list[str] = []
        if initial_rpc_response is not None:
            first_payload = decode_search_creatives_response(await initial_rpc_response.text())
            first_rpc_hrefs = extract_creative_hrefs_from_rpc(first_payload, advertiser_url)
        else:
            await status("Không bắt được SearchCreatives ban đầu, tiếp tục bằng cuộn trang...")

        hrefs = await scroll_and_collect_links(
            page0,
            advertiser_url,
            cancelled,
            initial_hrefs=first_rpc_hrefs,
        )
        await ctx0.close()

        total = len(hrefs)
        await status(f"Tìm thấy {total} quảng cáo. Bắt đầu lọc các quảng cáo có YouTube ID...")

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
            if data.get("youtube_link"):
                rows.append(data)

            if on_progress:
                await on_progress(i, total, cid_str)

        await browser.close()
        await status(f"Đã lọc được {len(rows)} quảng cáo có YouTube ID.")

    # Bước 3: Xuất Excel
    df = pd.DataFrame(rows, columns=EXPORT_COLUMNS)
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

    return ScrapeResult(
        output_file=output_file,
        scanned_total=total,
        exported_total=len(rows),
    )


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
        print(
            f"\n✓ Done → {result.output_file} "
            f"({result.exported_total}/{result.scanned_total} quảng cáo có YouTube ID)"
        )

    asyncio.run(_cli())
