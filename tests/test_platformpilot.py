from fastapi.testclient import TestClient

from platformpilot import runner, scenarios
from platformpilot.server import app


def test_forty_scenarios_unique():
    sc = scenarios.all_scenarios()
    assert len(sc) == 40 and len({s.id for s in sc}) == 40


def test_eval_meets_floor():
    s = runner.run_eval("offline")["summary"]
    assert s["n"] == 40 and s["root_cause_accuracy"] >= 0.85 and s["mttd_agent_min"] < s["mttd_manual_min"]


def test_approval_flow_heals():
    c = TestClient(app)
    rid = c.post("/api/runs", json={"scenario_id": "oom_killed-0", "mode": "offline"}).json()["id"]
    import time
    for _ in range(50):
        r = c.get(f"/api/runs/{rid}").json()
        if r["status"] != "running":
            break
        time.sleep(0.1)
    assert r["status"] == "awaiting_approval" and r["diagnosis"]["category"] == "oom_killed"
    assert c.post(f"/api/runs/{rid}/approve").json()["status"] == "resolved"
    assert b"platformpilot_runs_total" in c.get("/metrics").content


def test_agent_does_not_mutate_before_approval():
    sc = scenarios.get("missing_secret-1")
    from platformpilot.agent import OfflineAgent
    from platformpilot.tools import SimCluster
    cl = SimCluster(sc)
    OfflineAgent().investigate(sc.alert, sc.namespace, cl, lambda s: None)
    assert not sc.healed
