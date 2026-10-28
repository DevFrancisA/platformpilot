"""Investigation agents.

* `LLMAgent`     - Claude tool-use loop (needs ANTHROPIC_API_KEY).
* `OfflineAgent` - deterministic planner that drives the *same tools* step by step, so the
                   project runs, demos and evaluates with zero credentials.

Both return a Diagnosis dict:
  {category, summary, evidence[], fix{action,target,params}, confidence}
The agent never writes to the cluster. Fixes are applied only after human approval.
"""
from __future__ import annotations

import json
import os
import re
from typing import Callable

from .scenarios import CATEGORIES
from .tools import TOOL_SPECS

StepCB = Callable[[dict], None]

FIX_ACTIONS = ["set_env", "set_memory_limit", "set_image", "create_or_rename_secret",
               "patch_configmap", "patch_readiness_probe", "reduce_memory_request", "expand_pvc"]

SYSTEM = f"""You are PlatformPilot, an SRE assistant. Investigate a Kubernetes alert using the read-only tools,
then call submit_diagnosis exactly once. Be efficient: gather the minimum evidence needed (typically 4-6 tool calls).
Check dependencies if an app can't reach another service. Root-cause categories: {CATEGORIES}.
Fix actions: {FIX_ACTIONS}. You cannot change the cluster; a human approves your proposed fix."""

SUBMIT_TOOL = {
    "name": "submit_diagnosis", "description": "Submit the final root cause and proposed fix.",
    "input_schema": {"type": "object", "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "summary": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
        "fix": {"type": "object", "properties": {"action": {"type": "string", "enum": FIX_ACTIONS},
                "target": {"type": "string"}, "params": {"type": "object"}}, "required": ["action", "target"]}},
        "required": ["category", "summary", "evidence", "fix", "confidence"]},
}


class LLMAgent:
    name = "llm"

    def __init__(self, model: str | None = None):
        import anthropic
        self.client = anthropic.Anthropic()
        self.model = model or os.getenv("PLATFORMPILOT_MODEL", "claude-sonnet-5-5")

    def investigate(self, alert: str, namespace: str, cluster, on_step: StepCB) -> dict:
        msgs = [{"role": "user", "content": f"Alert: {alert}\nNamespace: {namespace}"}]
        for _ in range(10):
            r = self.client.messages.create(model=self.model, max_tokens=1500, system=SYSTEM,
                                            tools=TOOL_SPECS + [SUBMIT_TOOL], messages=msgs)
            msgs.append({"role": "assistant", "content": r.content})
            results = []
            for b in r.content:
                if b.type == "text" and b.text.strip():
                    on_step({"kind": "thought", "text": b.text.strip()})
                if b.type != "tool_use":
                    continue
                if b.name == "submit_diagnosis":
                    return b.input
                out = cluster.call(b.name, b.input)
                on_step({"kind": "tool", "tool": b.name, "args": b.input, "result": out})
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(out)})
            if not results:
                break
            msgs.append({"role": "user", "content": results})
        return {"category": "unknown", "summary": "Agent did not converge.", "evidence": [], "confidence": 0.0,
                "fix": {"action": "none", "target": ""}}


class OfflineAgent:
    name = "offline"

    def investigate(self, alert: str, namespace: str, cluster, on_step: StepCB) -> dict:
        def tool(name, thought, **args):
            on_step({"kind": "thought", "text": thought})
            out = cluster.call(name, args)
            on_step({"kind": "tool", "tool": name, "args": args, "result": out})
            return out

        pods = tool("list_pods", "Start by finding which pods are unhealthy.")["pods"]
        bad = [p for p in pods if p["status"] != "Running" or p["ready"] != "1/1"]
        if not bad:
            return self._unknown("No unhealthy pods found.")
        # the alert names the pod; fall back to first unhealthy
        m = re.search(r"([a-z0-9-]+-[0-9a-f]{3,5}-[a-z]\d+)", alert)
        primary = next((p for p in bad if m and p["name"] == m.group(1)), bad[0])
        pn = primary["name"]
        ev = tool("get_events", f"{pn} is {primary['status']}; check cluster events for the cause.")["events"]
        warn = [e for e in ev if e["type"] == "Warning" and pn in e["object"]]
        etext = " ".join(e["message"] for e in warn)
        evidence = [f"{pn}: status={primary['status']} ready={primary['ready']} restarts={primary['restarts']}"]
        evidence += [f"event {e['reason']}: {e['message']}" for e in warn[:3]]
        svc = pn.rsplit("-", 2)[0]
        target = f"deployment/{svc}"

        def done(cat, summary, fix, conf):
            return {"category": cat, "summary": summary, "evidence": evidence, "fix": fix, "confidence": conf}

        # --- failures fully explained by events (container never started)
        if primary["status"] in ("ImagePullBackOff", "ErrImagePull") or "manifest unknown" in etext:
            img = re.search(r'pull image "([^"]+)"', etext)
            img = img.group(1) if img else "?"
            return done("bad_image_tag", f"Image {img} does not exist in the registry.",
                        {"action": "set_image", "target": target, "params": {"image": img.split(":")[0] + ":<last-known-good>"}}, 0.97)
        if primary["status"] == "CreateContainerConfigError" or re.search(r'secret "[^"]+" not found', etext):
            sec = re.search(r'secret "([^"]+)" not found', etext)
            sec = sec.group(1) if sec else "?"
            return done("missing_secret", f"Pod references secret '{sec}' which does not exist.",
                        {"action": "create_or_rename_secret", "target": f"secret/{sec}", "params": {"namespace": namespace, "name": sec}}, 0.97)
        if primary["status"] == "Pending" and "Insufficient" in etext:
            cfg = tool("get_deployment_config", "Scheduler says insufficient resources; inspect the pod's requests.")
            req = cfg["deployment"]["resources"]["requests"]["memory"]
            evidence.append(f"memory request={req}; nodes have no capacity")
            return done("insufficient_resources", f"Pod requests {req} memory but no node can fit it.",
                        {"action": "reduce_memory_request", "target": target, "params": {"memory": "512Mi"}}, 0.94)

        # --- container started: need describe + logs
        d = tool("describe_pod", f"{pn} started; check last termination state.", pod=pn)
        logs = tool("get_pod_logs", "Read the application's own logs.", pod=pn)["lines"]
        ltext = "\n".join(logs)
        last = d.get("last_state") or {}
        evidence += [f"last_state={last.get('reason')} exit={last.get('exit_code')}"] + [f"log: {l}" for l in logs[-3:]]

        if last.get("reason") == "OOMKilled" or "OOMKilling" in etext:
            mem = tool("get_cloudwatch_metrics", "OOM suspected; confirm memory trend in CloudWatch.", metric="MemoryUtilization")["datapoints"]
            evidence.append(f"MemoryUtilization trend: {mem[0]}% -> {mem[-1]}%")
            lim = d["limits"]["memory"]
            new = f"{int(re.sub(r'[^0-9]', '', lim)) * 2}Mi"
            return done("oom_killed", f"Container exceeded its {lim} memory limit and was OOM-killed (exit 137).",
                        {"action": "set_memory_limit", "target": target, "params": {"memory": new}}, 0.95)

        m = re.search(r"required environment variable (\w+) is not set", ltext)
        if m:
            return done("missing_env_var", f"Required env var {m.group(1)} is not set on the deployment.",
                        {"action": "set_env", "target": target, "params": {"name": m.group(1), "value": "<value-from-secret>"}}, 0.96)

        m = re.search(r"config key (\w+): (.+)", ltext)
        if m:
            cfg = tool("get_deployment_config", "Startup fails parsing config; look at the ConfigMap value.")
            cm = cfg["deployment"]["configmap"]
            evidence.append(f"configmap {cm}: {m.group(1)}={cfg['configmaps'][cm].get(m.group(1))!r}")
            defaults = {"PORT": "8080", "TIMEOUT_MS": "3000", "FEATURE_X": "true"}
            return done("bad_config_value", f"ConfigMap {cm} has an unparsable value for {m.group(1)}.",
                        {"action": "patch_configmap", "target": f"configmap/{cm}", "params": {"key": m.group(1), "value": defaults.get(m.group(1), "<fix>")}}, 0.95)

        m = re.search(r"(?:dial tcp |\b)([a-z]+):(\d+): connect: connection refused|(\w+) unreachable", ltext)
        if m:
            dep = m.group(1) or m.group(3)
            dp = next((p for p in pods if p["name"].startswith(dep + "-")), None)
            if dp:
                dl = tool("get_pod_logs", f"{pn} cannot reach {dep}; check the {dep} pod itself.", pod=dp["name"])["lines"]
                evidence += [f"{dp['name']} ({dp['status']}): {l}" for l in dl[-2:]]
                if any("no space left" in l for l in dl):
                    return done("dependency_down", f"Dependency {dep} is down: its volume is full, so {pn} cannot connect.",
                                {"action": "expand_pvc", "target": f"statefulset/{dep}", "params": {"pvc": f"data-{dp['name']}", "size": "+20Gi"}}, 0.93)
                return done("dependency_down", f"Dependency {dep} is unhealthy.",
                            {"action": "expand_pvc", "target": f"statefulset/{dep}", "params": {}}, 0.6)

        if "Readiness probe failed" in etext and "404" in etext:
            cfg = tool("get_deployment_config", "Readiness probe returns 404; compare probe path with served routes.")["deployment"]
            probe, served = cfg["readiness_probe"]["path"], cfg["app_serves"]
            evidence.append(f"probe path {probe} not in served routes {served}")
            fixp = next((r for r in served if "health" in r), "/health")
            return done("probe_misconfigured", f"Readiness probe hits {probe} which the app does not serve (404).",
                        {"action": "patch_readiness_probe", "target": target, "params": {"path": fixp}}, 0.93)

        return self._unknown("Evidence did not match a known failure pattern.", evidence)

    @staticmethod
    def _unknown(msg, evidence=None):
        return {"category": "unknown", "summary": msg, "evidence": evidence or [], "confidence": 0.1,
                "fix": {"action": "none", "target": ""}}


def make_agent(mode: str | None = None):
    mode = mode or os.getenv("PLATFORMPILOT_AGENT", "auto")
    if mode == "llm" or (mode == "auto" and os.getenv("ANTHROPIC_API_KEY")):
        return LLMAgent()
    return OfflineAgent()
