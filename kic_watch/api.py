"""Background monitor service. Run with: uvicorn kic_watch.api:app --host 127.0.0.1."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()

from kic_agent import seed_assumptions
from kic_watch.monitor import monitor_portfolio
from kic_watch.security import safe_error
from storage import load_state, save_assumption

INTERVAL_SECONDS = max(60, int(os.getenv("MONITOR_INTERVAL_SECONDS", "900")))
scheduler: Optional[AsyncIOScheduler] = None
last_run: Optional[dict] = None


async def _poll_portfolio():
    global last_run
    started = datetime.now(timezone.utc).isoformat()
    try:
        result = await asyncio.to_thread(monitor_portfolio)
        last_run = {"started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
                    "status": "completed", "tickers_scanned": result.get("tickers_scanned", 0),
                    "new_reports": result.get("new_reports", 0), "errors": result.get("errors", 0)}
    except Exception as exc:
        last_run = {"started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
                    "status": "failed", "error": safe_error(exc)}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler
    assumptions, _ = load_state()
    if not assumptions:
        for assumption in seed_assumptions():
            save_assumption(assumption)
    scheduler = AsyncIOScheduler(timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1})
    scheduler.add_job(_poll_portfolio, "interval", seconds=INTERVAL_SECONDS, id="portfolio-poll",
                      next_run_time=datetime.now(timezone.utc), replace_existing=True)
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)
    scheduler = None


app = FastAPI(title="KIC Assumption Monitor", version="0.2.0", lifespan=lifespan)


@app.get("/")
def root():
    """Point local operators to the service status and API documentation."""
    return {"service": app.title, "health": "/health", "docs": "/docs"}


@app.get("/health")
def health():
    job = scheduler.get_job("portfolio-poll") if scheduler and scheduler.running else None
    return {"status": "ok" if scheduler and scheduler.running else "starting",
            "scheduler_running": bool(scheduler and scheduler.running),
            "interval_seconds": INTERVAL_SECONDS,
            "next_run_at": job.next_run_time.isoformat() if job and job.next_run_time else None,
            "last_run": last_run}
