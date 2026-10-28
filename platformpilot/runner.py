"""Run orchestration, Prometheus metrics, and the evaluation suite."""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

import psutil
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from . import scenarios
from .agent import make_agent
from .tools import SimCluster

REGISTRY = CollectorRegistry()
RUNS = Counter("platformpilot_runs_total", "Agent investigations", ["outcome"], registry=REGISTRY)
DURATION = Histogram("platformpilot_diagnosis_seconds", "Agent time to diagnosis", registry=REGISTRY,
                     buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60))
TOOLCALLS = Counter("platformpilot_tool_calls_total", "Tool calls", ["tool"], registry=REGISTRY)
ACCURACY = Gauge("platformpilot_eval_root_cause_accuracy", "Latest eval root-cause accuracy (0-1)", registry=REGISTRY)
FIX_ACC = Gauge("platformpilot_eval_fix_accuracy", "Latest eval proposed-fix accuracy (0-1)", registry=REGISTRY)
MTTD_AGENT = Gauge("platformpilot_eval_mttd_minutes", "Mean time to diagnose with agent (min)", registry=REGISTRY)
MTTD_MANUAL = Gauge("platformpilot_eval_mttd_manual_minutes", "Modeled manual baseline MTTD (min)", registry=REGISTRY)
CPU = Gauge("platformpilot_process_cpu_percent", "Agent process CPU %", registry=REGISTRY)
RSS = Gauge("platformpilot_process_rss_mb", "Agent process RSS MB", registry=REGISTRY)
_proc = psutil.Process()

# Manual-baseline model (see README "Methodology"). Documented assumptions, not measurements.
HUMAN_ACK_TRIAGE_MIN = 4.0     # page ack, open terminal, find the right cluster/namespace
HUMAN_STEP_MIN = 2.5           # one kubectl / console / CloudWatch lookup incl. context switching
HUMAN_APPROVE_MIN = 1.0        # engineer reads agent diagnosis and approves the fix

RESULTS = Path(__file__).resolve().parent.parent / "eval_results.json"
_runs: dict[str, dict] = {}
_lock = threading.Lock()


def update_resources():
    CPU.set(_proc.cpu_percent(interval=None))
    RSS.set(_proc.memory_info().rss / 1e6)


def _investigate(run: dict, scenario, agent, latency: float):
    cluster = SimCluster(scenario, latency=latency)
    run["_cluster"] = cluster
    t0 = time.perf_counter()

    def on_step(step):
        step["t"] = round(time.perf_counter() - t0, 2)
        run["steps"].append(step)
        if step["kind"] == "tool":
            TOOLCALLS.labels(step["tool"]).inc()

    try:
        dx = agent.investigate(scenario.alert, scenario.namespace, cluster, on_step)
        run["diagnosis"] = dx
        run["status"] = "awaiting_approval" if dx.get("fix", {}).get("action") not in (None, "none") else "inconclusive"
    except Exception as e:  # surface agent failure instead of hanging the UI
        run["status"], run["error"] = "error", str(e)
    run["agent_seconds"] = round(time.perf_counter() - t0, 3)
    run["tool_calls"] = cluster.calls
    DURATION.observe(run["agent_seconds"])
    RUNS.labels(run["status"]).inc()
    update_resources()


def start_run(scenario_id: str, latency: float = 0.3, mode: str | None = None) -> dict:
    sc = scenarios.get(scenario_id)
    agent = make_agent(mode)
    run = {"id": uuid.uuid4().hex[:8], "scenario": sc.public(), "agent": agent.name, "status": "running",
           "steps": [], "diagnosis": None, "apply": None}
    with _lock:
        _runs[run["id"]] = run
    threading.Thread(target=_investigate, args=(run, sc, agent, latency), daemon=True).start()
    return run


def get_run(run_id: str) -> dict | None:
    r = _runs.get(run_id)
    return {k: v for k, v in r.items() if not k.startswith("_")} if r else None


def decide(run_id: str, approve: bool) -> dict | None:
    r = _runs.get(run_id)
    if not r or r["status"] != "awaiting_approval":
        return get_run(run_id) if r else None
    if not approve:
        r["status"] = "rejected"
        RUNS.labels("rejected").inc()
    else:
        r["apply"] = r["_cluster"].apply_fix(r["diagnosis"]["fix"])
        r["status"] = "resolved" if r["apply"]["healed"] else "fix_failed"
        RUNS.labels(r["status"]).inc()
    return get_run(run_id)


def run_eval(mode: str | None = None, seed: int = 7) -> dict:
    """Run every scenario headless; compare to ground truth; persist results."""
    agent = make_agent(mode)
    rows = []
    for sc in scenarios.all_scenarios(seed):
        run = {"steps": []}
        _investigate(run, sc, agent, latency=0.0)
        dx = run.get("diagnosis") or {}
        cluster = run["_cluster"]
        cat_ok = dx.get("category") == sc.category
        fix_ok = dx.get("fix", {}).get("action") == sc.truth_fix["action"]
        healed = cluster.apply_fix(dx["fix"])["healed"] if dx.get("fix", {}).get("action") not in (None, "none") else False
        human_min = HUMAN_ACK_TRIAGE_MIN + HUMAN_STEP_MIN * _min_human_steps(sc) + HUMAN_APPROVE_MIN * 0
        agent_min = run["agent_seconds"] / 60 + HUMAN_APPROVE_MIN
        rows.append({"id": sc.id, "category": sc.category, "hard": sc.hard, "predicted": dx.get("category"),
                     "root_cause_correct": cat_ok, "fix_correct": fix_ok, "healed": healed,
                     "tool_calls": run["tool_calls"], "agent_seconds": run["agent_seconds"],
                     "manual_minutes": round(human_min, 1), "agent_minutes": round(agent_min, 2)})
    n = len(rows)
    mean = lambda k: round(sum(r[k] for r in rows) / n, 3)
    summary = {
        "agent": agent.name, "model": getattr(agent, "model", None), "n": n,
        "root_cause_accuracy": round(sum(r["root_cause_correct"] for r in rows) / n, 3),
        "fix_accuracy": round(sum(r["fix_correct"] for r in rows) / n, 3),
        "remediation_success": round(sum(r["healed"] for r in rows) / n, 3),
        "hard_accuracy": round(sum(r["root_cause_correct"] for r in rows if r["hard"]) / max(1, sum(r["hard"] for r in rows)), 3),
        "mean_tool_calls": mean("tool_calls"), "mean_agent_seconds": mean("agent_seconds"),
        "mttd_manual_min": mean("manual_minutes"), "mttd_agent_min": mean("agent_minutes"),
        "assumptions": {"ack_triage_min": HUMAN_ACK_TRIAGE_MIN, "per_step_min": HUMAN_STEP_MIN,
                        "approve_min": HUMAN_APPROVE_MIN},
        "by_category": {c: round(sum(r["root_cause_correct"] for r in rows if r["category"] == c) / 5, 2) for c in scenarios.CATEGORIES},
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    out = {"summary": summary, "rows": rows}
    RESULTS.write_text(json.dumps(out, indent=2))
    ACCURACY.set(summary["root_cause_accuracy"]); FIX_ACC.set(summary["fix_accuracy"])
    MTTD_AGENT.set(summary["mttd_agent_min"]); MTTD_MANUAL.set(summary["mttd_manual_min"])
    return out


def _min_human_steps(sc) -> int:
    """Lookups a human SRE needs by hand for this failure class (pods, events, then class-specific)."""
    base = {"bad_image_tag": 2, "missing_secret": 2, "insufficient_resources": 4, "missing_env_var": 4,
            "oom_killed": 5, "bad_config_value": 5, "probe_misconfigured": 5, "dependency_down": 6}[sc.category]
    return base + (1 if sc.hard else 0)


def load_results() -> dict | None:
    if RESULTS.exists():
        out = json.loads(RESULTS.read_text())
        s = out["summary"]
        ACCURACY.set(s["root_cause_accuracy"]); FIX_ACC.set(s["fix_accuracy"])
        MTTD_AGENT.set(s["mttd_agent_min"]); MTTD_MANUAL.set(s["mttd_manual_min"])
        return out
    return None
