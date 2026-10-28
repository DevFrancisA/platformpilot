"""FastAPI app: local UI + JSON API + Prometheus /metrics."""
from __future__ import annotations

import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import runner, scenarios

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="PlatformPilot")
_eval_state = {"running": False, "error": None}


class RunReq(BaseModel):
    scenario_id: str
    mode: str | None = None


class EvalReq(BaseModel):
    mode: str | None = None


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/scenarios")
def list_scenarios():
    return [s.public() | {"category_hint": None} for s in scenarios.all_scenarios()]


@app.post("/api/runs")
def create_run(req: RunReq):
    try:
        return {"id": runner.start_run(req.scenario_id, mode=req.mode)["id"]}
    except KeyError:
        raise HTTPException(404, "unknown scenario")


@app.get("/api/runs/{run_id}")
def read_run(run_id: str):
    r = runner.get_run(run_id)
    if not r:
        raise HTTPException(404)
    return r


@app.post("/api/runs/{run_id}/approve")
def approve(run_id: str):
    r = runner.decide(run_id, True)
    if not r:
        raise HTTPException(404)
    return r


@app.post("/api/runs/{run_id}/reject")
def reject(run_id: str):
    r = runner.decide(run_id, False)
    if not r:
        raise HTTPException(404)
    return r


def _bg_eval(mode):
    try:
        runner.run_eval(mode)
    except Exception as e:
        _eval_state["error"] = str(e)
    finally:
        _eval_state["running"] = False


@app.post("/api/eval")
def start_eval(req: EvalReq):
    if not _eval_state["running"]:
        _eval_state.update(running=True, error=None)
        threading.Thread(target=_bg_eval, args=(req.mode,), daemon=True).start()
    return _eval_state


@app.get("/api/eval")
def read_eval():
    return {"state": _eval_state, "results": runner.load_results()}


@app.get("/metrics")
def metrics():
    runner.update_resources()
    return Response(generate_latest(runner.REGISTRY), media_type=CONTENT_TYPE_LATEST)
