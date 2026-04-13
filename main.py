"""
main.py — FastAPI server: API + serve frontend
Chạy: uvicorn main:app --host 0.0.0.0 --port 8000
"""

import asyncio
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from scraper import run_scrape

app = FastAPI(title="Google Ads Scraper API")

OUTPUTS_DIR = Path(__file__).parent / "outputs"
OUTPUTS_DIR.mkdir(exist_ok=True)

JOBS_FILE = OUTPUTS_DIR / "jobs.json"

# job_id → { status, progress, total, message, file, error }
jobs: dict[str, dict] = {}

# job_id → threading.Event (set = yêu cầu huỷ)
cancel_events: dict[str, threading.Event] = {}


def _load_jobs():
    if JOBS_FILE.exists():
        try:
            jobs.update(json.loads(JOBS_FILE.read_text()))
        except Exception:
            pass


def _save_jobs():
    try:
        JOBS_FILE.write_text(json.dumps(jobs))
    except Exception:
        pass


_load_jobs()

_thread_pool = ThreadPoolExecutor(max_workers=2)


# ── Models ────────────────────────────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    url: str


# ── API Routes ────────────────────────────────────────────────────────────────

@app.post("/api/scrape")
async def start_scrape(req: ScrapeRequest):
    if not req.url.startswith("https://adstransparency.google.com"):
        raise HTTPException(400, "URL không hợp lệ. Phải là link Google Ads Transparency.")

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "running",
        "progress": 0,
        "total": 0,
        "message": "Đang khởi động...",
        "file": None,
        "error": None,
    }
    cancel_events[job_id] = threading.Event()

    asyncio.create_task(_run_job(job_id, req.url))
    return {"job_id": job_id}


@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job không tồn tại.")
    return job


@app.get("/api/download/{job_id}")
async def download_file(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job không tồn tại.")
    if job["status"] != "done" or not job["file"]:
        raise HTTPException(400, "File chưa sẵn sàng.")
    file_path = Path(job["file"])
    if not file_path.exists():
        raise HTTPException(404, "File không tìm thấy trên server.")
    return FileResponse(
        path=file_path,
        filename="ads_export.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/jobs")
async def list_jobs():
    return {
        job_id: job
        for job_id, job in jobs.items()
        if job["status"] == "running"
    }


@app.post("/api/cancel/{job_id}")
async def cancel_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job không tồn tại.")
    if job["status"] != "running":
        raise HTTPException(400, "Job không đang chạy.")
    event = cancel_events.get(job_id)
    if event:
        event.set()
    job["status"] = "cancelled"
    job["message"] = "Đã huỷ."
    _save_jobs()
    return {"ok": True}


# ── Background job ────────────────────────────────────────────────────────────

async def _run_job(job_id: str, url: str):
    job = jobs[job_id]
    output_file = OUTPUTS_DIR / f"{job_id}.xlsx"

    cancel_event = cancel_events.get(job_id)

    def _in_thread():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def on_progress(current: int, total: int, cid: str):
            job["progress"] = current
            job["total"] = total
            job["message"] = f"[{current}/{total}] Đang xử lý {cid}…"
            _save_jobs()

        async def on_status(message: str):
            job["message"] = message
            _save_jobs()

        def should_cancel() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        try:
            return loop.run_until_complete(
                run_scrape(url, output_file, on_progress, on_status, should_cancel)
            )
        finally:
            loop.close()

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(_thread_pool, _in_thread)
        if job["status"] != "cancelled":
            job["status"] = "done"
            job["file"] = str(output_file)
            job["message"] = f"Hoàn tất {job['total']} quảng cáo."
    except Exception as e:
        if job["status"] != "cancelled":
            job["status"] = "error"
            job["error"] = str(e)
            job["message"] = f"Lỗi: {e}"
    finally:
        cancel_events.pop(job_id, None)
        _save_jobs()


# ── Serve Frontend (PHẢI đặt cuối cùng) ──────────────────────────────────────
app.mount("/", StaticFiles(directory="static", html=True), name="static")
