"""Read-only investigation tools exposed to the agent, plus the gated fix applier.

`SimCluster` serves a Scenario snapshot. `LiveCluster` shells out to kubectl / boto3
for a real cluster (see README). Both implement the same interface.
"""
from __future__ import annotations

import json
import subprocess
import time

from .scenarios import Scenario

TOOL_SPECS = [
    {"name": "list_pods", "description": "List pods in the alert's namespace with status, readiness and restarts.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_events", "description": "Recent Kubernetes events in the namespace (warnings first).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_pod_logs", "description": "Tail the logs of a pod.",
     "input_schema": {"type": "object", "properties": {"pod": {"type": "string"}}, "required": ["pod"]}},
    {"name": "describe_pod", "description": "kubectl describe for a pod: last state, exit codes, restart reasons.",
     "input_schema": {"type": "object", "properties": {"pod": {"type": "string"}}, "required": ["pod"]}},
    {"name": "get_deployment_config", "description": "Deployment spec (image, env, resources, probes) and its ConfigMap.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_cloudwatch_metrics", "description": "CloudWatch Container Insights series (last 12 points) for MemoryUtilization, CPUUtilization or PodRestartCount.",
     "input_schema": {"type": "object", "properties": {"metric": {"type": "string",
                      "enum": ["MemoryUtilization", "CPUUtilization", "PodRestartCount"]}}, "required": ["metric"]}},
]
TOOL_NAMES = [t["name"] for t in TOOL_SPECS]


class SimCluster:
    def __init__(self, scenario: Scenario, latency: float = 0.0):
        self.s = scenario
        self.latency = latency
        self.calls = 0

    def call(self, name: str, args: dict | None = None) -> dict:
        args = args or {}
        if name not in TOOL_NAMES:
            return {"error": f"unknown tool {name}"}
        self.calls += 1
        if self.latency:
            time.sleep(self.latency)
        return getattr(self, name)(**args)

    # ---- read-only tools
    def list_pods(self):
        return {"namespace": self.s.namespace, "pods": self.s.pods}

    def get_events(self):
        ev = sorted(self.s.events, key=lambda e: (e["type"] != "Warning", e.get("time", "")))
        return {"events": ev}

    def get_pod_logs(self, pod: str):
        if pod not in self.s.logs:
            return {"error": f"pod {pod} not found"}
        lines = self.s.logs[pod]
        return {"pod": pod, "lines": lines if lines else ["<no logs: container has not started>"]}

    def describe_pod(self, pod: str):
        p = next((x for x in self.s.pods if x["name"] == pod), None)
        if not p:
            return {"error": f"pod {pod} not found"}
        return {"pod": pod, "status": p["status"], "restarts": p["restarts"],
                "last_state": p.get("last_state", {"reason": "Error" if p["restarts"] else None, "exit_code": 1 if p["restarts"] else None}),
                "limits": self.s.deployment["resources"]["limits"], "requests": self.s.deployment["resources"]["requests"]}

    def get_deployment_config(self):
        d = self.s.deployment
        return {"deployment": d, "configmaps": self.s.configmaps}

    def get_cloudwatch_metrics(self, metric: str):
        if metric not in self.s.metrics:
            return {"error": f"unknown metric {metric}"}
        return {"metric": metric, "period": "60s", "datapoints": self.s.metrics[metric]}

    # ---- gated write path (only invoked after human approval)
    def apply_fix(self, fix: dict) -> dict:
        """Simulated apply: succeeds only if the proposed fix matches the real defect."""
        ok = fix.get("action") == self.s.truth_fix["action"]
        if ok:
            self.s.healed = True
            for p in self.s.pods:
                if p["status"] != "Running" or p["ready"] != "1/1":
                    p.update(status="Running", ready="1/1")
        return {"applied": ok, "healed": self.s.healed,
                "detail": f"kubectl applied {fix.get('action')} to {fix.get('target')}" if ok
                else "fix applied but pod still unhealthy (wrong remediation)"}


class LiveCluster:  # pragma: no cover - requires a real cluster
    """kubectl + CloudWatch backed implementation of the same tool interface."""

    def __init__(self, namespace: str, cluster_name: str, region: str = "us-east-1", latency: float = 0.0):
        self.ns, self.cluster, self.region = namespace, cluster_name, region
        self.calls = 0

    def _k(self, *a):
        return subprocess.run(["kubectl", "-n", self.ns, *a], capture_output=True, text=True, timeout=30).stdout

    def call(self, name, args=None):
        args = args or {}
        self.calls += 1
        if name == "list_pods":
            return json.loads(self._k("get", "pods", "-o", "json"))["items"] and {"pods": [
                {"name": i["metadata"]["name"], "status": i["status"]["phase"]}
                for i in json.loads(self._k("get", "pods", "-o", "json"))["items"]]}
        if name == "get_events":
            return {"events": self._k("get", "events", "--sort-by=.lastTimestamp").splitlines()[-40:]}
        if name == "get_pod_logs":
            return {"lines": self._k("logs", args["pod"], "--tail=60", "--previous").splitlines()}
        if name == "describe_pod":
            return {"describe": self._k("describe", "pod", args["pod"])}
        if name == "get_deployment_config":
            return {"deployment": json.loads(self._k("get", "deploy", "-o", "json"))}
        if name == "get_cloudwatch_metrics":
            import datetime as dt
            import boto3
            cw = boto3.client("cloudwatch", region_name=self.region)
            now = dt.datetime.utcnow()
            r = cw.get_metric_statistics(Namespace="ContainerInsights",
                                         MetricName={"MemoryUtilization": "pod_memory_utilization", "CPUUtilization": "pod_cpu_utilization",
                                                     "PodRestartCount": "pod_number_of_container_restarts"}[args["metric"]],
                                         Dimensions=[{"Name": "ClusterName", "Value": self.cluster}, {"Name": "Namespace", "Value": self.ns}],
                                         StartTime=now - dt.timedelta(minutes=15), EndTime=now, Period=60, Statistics=["Average"])
            return {"datapoints": [round(p["Average"], 1) for p in sorted(r["Datapoints"], key=lambda p: p["Timestamp"])]}
        return {"error": "unknown tool"}

    def apply_fix(self, fix):
        return {"applied": False, "detail": "Live apply is intentionally not automated; run the suggested kubectl command yourself."}
