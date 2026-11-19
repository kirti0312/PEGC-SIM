"""Step 8: FastAPI backend. Run from backend/:  uvicorn api.main:app --port 8000"""
from __future__ import annotations

import asyncio
import os
import shutil
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

from . import service as sv
from .service import ENGINE, InputError, RunManager

NODE = Literal["W0", "E0", "W1", "E1", "N0", "S0", "N1", "S1", "N2", "S2"]


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy: Literal["A", "B", "C", "C1", "C2", "C3"] = "C"
    origin: NODE = "W0"
    dest: NODE = "E1"
    density: Literal["low", "medium", "high", "custom"] = "medium"
    rate: float | None = Field(None, ge=100, le=900, description="veh/h per entry; only for density=custom (raw, not calibrated)")
    seed: int = Field(7, strict=True)
    blocked: list[str] = Field(default_factory=list, max_length=10)


def create_app(runner=None, calibration_path: str | None = None, snapshot_interval: float = 0.125,
               c3_path: str | None = None) -> FastAPI:
    app = FastAPI(title="PEGC SUMO backend", version="step8")
    origins = [o for o in os.environ.get("PEGC_CORS_ORIGINS",
               "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173").split(",") if o]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_origin_regex=r"https://.*\.app\.github\.dev",
                       allow_methods=["*"], allow_headers=["*"])
    mgr = RunManager(runner, snapshot_interval)
    if c3_path:
        mgr.c3 = sv.load_c3(c3_path)
    cal_path = calibration_path or sv.CALIBRATION_PATH

    def prepare(origin, dest, density, rate, seed, blocked):
        try:
            return sv.resolve(origin, dest, density, rate, seed, blocked, sv.load_calibration(cal_path))
        except InputError as e:
            raise HTTPException(422, str(e))

    def submit(kind, strategies, scenario, meta):
        try:
            return mgr.submit(kind, strategies, scenario, meta)
        except sv.Busy as e:
            raise HTTPException(429, str(e))

    @app.get("/api/health")
    def health():
        cal = sv.load_calibration(cal_path)
        try:
            from pegc_sumo.sumo_io import find_binary
            find_binary("sumo")
            sumo = True
        except Exception:  # noqa: BLE001
            sumo = shutil.which("sumo") is not None
        return {"status": "ok", "engine": ENGINE, "sumo_found": sumo, "calibration_scale": cal["scale"],
                "calibration_status": cal["status"], "strategies": list(sv.STRATEGIES), "boundary_nodes": sv.boundary_nodes(),
                "high_traffic": "oversaturated by design", "queue": mgr.busy()}

    @app.post("/api/runs", status_code=202)
    def start_run(req: RunRequest):
        scenario, meta = prepare(req.origin, req.dest, req.density, req.rate, req.seed, req.blocked)
        run = submit("run", (req.strategy,), scenario, {**meta, "strategy": req.strategy})
        return {"id": run.id, "status": run.status, **run.meta}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        run = mgr.get(run_id)
        if run is None or run.kind != "run":
            raise HTTPException(404, "unknown run id")
        return {"id": run.id, "status": run.status, **run.meta, "summary": run.summary, "error": run.error}

    @app.get("/api/compare")
    def compare(origin: NODE = "W0", dest: NODE = "E1", density: Literal["low", "medium", "high", "custom"] = "medium",
                rate: float | None = Query(None, ge=100, le=900), seed: int = 7,
                blocked: str = Query("", description="comma-separated segment ids"),
                timeout: float = Query(900, gt=0, le=3600)):
        scenario, meta = prepare(origin, dest, density, rate, seed, [b for b in blocked.split(",") if b])
        run = submit("compare", sv.COMPARE_STRATEGIES, scenario, meta)
        if not run.done.wait(timeout):
            raise HTTPException(504, f"compare still {run.status} after {timeout}s; the runs continue in the background")
        if run.status != "done":
            raise HTTPException(500, run.error or "compare failed")
        return {"status": "done", **run.meta, "strategies": list(sv.COMPARE_STRATEGIES), "run_order": "A, B, C one after another",
                "results": run.results, "percent_change_vs_A": sv.deltas_vs_a(run.results)}

    @app.websocket("/ws/runs/{run_id}")
    async def ws_run(ws: WebSocket, run_id: str):
        run = mgr.get(run_id)
        await ws.accept()
        if run is None or run.kind != "run":
            await ws.send_json({"type": "error", "engine": ENGINE, "detail": "unknown run id"})
            await ws.close(code=4404)
            return
        sent = 0
        try:
            while True:
                finished = run.done.is_set()
                if sent < len(run.snapshots):
                    await ws.send_json({"type": "snapshot", **run.snapshots[sent]})
                    sent += 1
                    await asyncio.sleep(snapshot_interval)
                    continue
                if finished:
                    break
                await asyncio.sleep(0.05)
            if run.status == "done":
                await ws.send_json({"type": "summary", "id": run.id, "status": "done", **run.meta, "summary": run.summary,
                                    "log": run.log or []})
            else:
                await ws.send_json({"type": "error", "engine": ENGINE, "id": run.id, "detail": run.error})
            await ws.close()
        except Exception:  # noqa: BLE001 - client went away
            pass

    return app


app = create_app()
