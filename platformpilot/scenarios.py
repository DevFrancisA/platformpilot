"""Simulated Kubernetes failure scenarios.

Each scenario is a self-contained fake cluster snapshot (pods, events, logs,
metrics, deployment config) plus ground truth used by the evaluation suite.
40 scenarios = 8 failure categories x 5 variants, including noisy / misleading
variants so diagnosis is not a pure keyword lookup.
"""
from __future__ import annotations

import copy
import random
from dataclasses import dataclass, field

CATEGORIES = [
    "missing_env_var",
    "oom_killed",
    "bad_image_tag",
    "missing_secret",
    "bad_config_value",
    "probe_misconfigured",
    "insufficient_resources",
    "dependency_down",
]

SERVICES = ["checkout", "payments", "inventory", "search", "auth", "notifier", "catalog", "orders"]
ENV_VARS = ["DATABASE_URL", "REDIS_HOST", "STRIPE_API_BASE", "KAFKA_BROKERS", "JWT_ISSUER", "S3_BUCKET"]
DEPS = [("redis", 6379), ("postgres", 5432), ("rabbitmq", 5672), ("kafka", 9092), ("memcached", 11211)]


@dataclass
class Scenario:
    id: str
    category: str
    namespace: str
    service: str
    alert: str
    pods: list[dict]
    logs: dict[str, list[str]]
    events: list[dict]
    metrics: dict[str, list[float]]
    deployment: dict
    configmaps: dict[str, dict]
    truth_fix: dict
    hard: bool = False
    healed: bool = field(default=False)

    def public(self) -> dict:
        return {"id": self.id, "namespace": self.namespace, "service": self.service,
                "alert": self.alert, "hard": self.hard}


def _ts(rng: random.Random, i: int) -> str:
    return f"2026-10-28T09:{(i * 2 + rng.randint(0, 1)) % 60:02d}:{rng.randint(0, 59):02d}Z"


def _series(rng, base, jitter, n=12, ramp=0.0):
    return [round(max(0.0, base + ramp * i + rng.uniform(-jitter, jitter)), 1) for i in range(n)]


def _noise_logs(rng, svc, n=3):
    pool = [
        f"INFO  {svc}: loaded feature flags (12 enabled)",
        f"WARN  {svc}: deprecated header X-Legacy-Auth still in use by 3 clients",
        f"INFO  {svc}: metrics exporter listening on :9102",
        f"DEBUG {svc}: gc pause 11ms",
        f"WARN  {svc}: slow query 212ms (threshold 200ms)",
    ]
    return rng.sample(pool, n)


def _noise_events(rng, ns, i):
    pool = [
        {"type": "Warning", "reason": "FailedMount", "object": f"pod/log-shipper-{rng.randint(10,99)}",
         "message": "MountVolume.SetUp failed for volume 'tmp': transient, retrying (resolved)"},
        {"type": "Normal", "reason": "ScalingReplicaSet", "object": f"deployment/unrelated-{rng.randint(1,9)}",
         "message": "Scaled up replica set to 3"},
        {"type": "Warning", "reason": "BackOff", "object": "pod/cron-cleanup-28812",
         "message": "Back-off restarting failed container (job already completed)"},
    ]
    return [dict(e, time=_ts(rng, i)) for e in rng.sample(pool, 2)]


def _base(rng, cat, idx, svc, hard):
    ns = rng.choice(["prod", "staging", "prod-eu"])
    pod = f"{svc}-{rng.randint(1000, 9999):x}-{rng.choice('abcdefgh')}{rng.randint(10, 99)}"
    sidecar = f"{svc}-sidecar-{rng.randint(10, 99)}"
    dep = {
        "name": svc, "image": f"registry.internal/{svc}:1.{rng.randint(4, 9)}.{rng.randint(0, 9)}",
        "replicas": 2, "env": {"LOG_LEVEL": "info", "PORT": "8080"},
        "env_from_secrets": [f"{svc}-creds"], "configmap": f"{svc}-config",
        "resources": {"requests": {"cpu": "250m", "memory": "256Mi"}, "limits": {"cpu": "500m", "memory": "512Mi"}},
        "readiness_probe": {"path": "/health", "port": 8080, "initial_delay_s": 5},
        "app_serves": ["/health", "/metrics", "/api"],
    }
    cm = {f"{svc}-config": {"PORT": "8080", "TIMEOUT_MS": "3000", "FEATURE_X": "true"}}
    pods = [{"name": pod, "ready": "0/1", "status": "Running", "restarts": 0, "age": "12m"},
            {"name": sidecar, "ready": "1/1", "status": "Running", "restarts": 0, "age": "12m"}]
    logs = {pod: [], sidecar: [f"INFO envoy: all upstreams healthy", f"INFO envoy: config reload ok"]}
    metrics = {"MemoryUtilization": _series(rng, 45, 5), "CPUUtilization": _series(rng, 30, 8),
               "PodRestartCount": [0.0] * 12}
    events = _noise_events(rng, ns, idx)
    return ns, pod, dep, cm, pods, logs, metrics, events


def build(cat: str, variant: int, seed: int) -> Scenario:
    rng = random.Random(seed * 1000 + variant * 17 + CATEGORIES.index(cat))
    svc = SERVICES[(variant + CATEGORIES.index(cat) * 3) % len(SERVICES)]
    hard = variant >= 3  # variants 3,4 of every category are the noisy/misleading ones
    ns, pod, dep, cm, pods, logs, metrics, events = _base(rng, cat, variant, svc, hard)
    sid = f"{cat}-{variant}"
    P = pods[0]
    fix: dict

    if cat == "missing_env_var":
        var = ENV_VARS[(variant + 1) % len(ENV_VARS)]
        P.update(status="CrashLoopBackOff", restarts=rng.randint(4, 9))
        logs[pod] = [f"INFO  {svc}: starting v{dep['image'].split(':')[1]}",
                     f"ERROR {svc}: required environment variable {var} is not set",
                     f"FATAL {svc}: configuration invalid, exiting with code 1"]
        if hard:  # add a misleading dependency-looking line
            logs[pod].insert(1, f"WARN  {svc}: could not resolve dependency host (skipping, config pending)")
        events += [{"type": "Warning", "reason": "BackOff", "object": f"pod/{pod}",
                    "message": "Back-off restarting failed container", "time": _ts(rng, 3)}]
        metrics["PodRestartCount"] = _series(rng, 0, 0.1, ramp=0.6)
        fix = {"action": "set_env", "target": f"deployment/{svc}", "params": {"name": var, "value": "<value-from-secret>"}}
        alert = f"KubePodCrashLooping: {ns}/{pod} restarting ({P['restarts']} in 15m)"

    elif cat == "oom_killed":
        lim = rng.choice([128, 256, 512])
        dep["resources"]["limits"]["memory"] = f"{lim}Mi"
        P.update(status="CrashLoopBackOff", restarts=rng.randint(3, 11))
        metrics["MemoryUtilization"] = _series(rng, 70, 3, ramp=2.4)
        metrics["MemoryUtilization"][-3:] = [97.0, 99.0, 100.0]
        metrics["PodRestartCount"] = _series(rng, 0, 0.1, ramp=0.5)
        logs[pod] = [f"INFO  {svc}: warming cache ({rng.randint(40, 90)}k entries)",
                     f"WARN  {svc}: heap usage 94%"]
        if hard:  # misleading timeout noise in the log, no explicit OOM line
            logs[pod] += [f"ERROR {svc}: upstream request timeout after 3000ms", f"WARN  {svc}: retrying (attempt 3/3)"]
        events += [{"type": "Warning", "reason": "BackOff", "object": f"pod/{pod}",
                    "message": "Back-off restarting failed container", "time": _ts(rng, 4)}]
        P["last_state"] = {"reason": "OOMKilled", "exit_code": 137}
        if not hard:
            events.append({"type": "Warning", "reason": "OOMKilling", "object": f"pod/{pod}",
                           "message": f"Memory cgroup out of memory: Killed process (limit {lim}Mi)", "time": _ts(rng, 4)})
        fix = {"action": "set_memory_limit", "target": f"deployment/{svc}", "params": {"memory": f"{lim * 2}Mi"}}
        alert = f"KubePodCrashLooping: {ns}/{pod} restarting ({P['restarts']} in 15m)"

    elif cat == "bad_image_tag":
        bad = f"{dep['image'].split(':')[0]}:{rng.choice(['1.99.0', 'latst', 'v2-rc9', '2.0.0-nonexistent'])}"
        dep["image"] = bad
        P.update(status="ImagePullBackOff", ready="0/1", restarts=0)
        logs[pod] = []  # container never started
        events += [{"type": "Warning", "reason": "Failed", "object": f"pod/{pod}",
                    "message": f"Failed to pull image \"{bad}\": manifest unknown: manifest tagged by \"{bad.split(':')[1]}\" is not found",
                    "time": _ts(rng, 2)},
                   {"type": "Warning", "reason": "BackOff", "object": f"pod/{pod}",
                    "message": f"Back-off pulling image \"{bad}\"", "time": _ts(rng, 3)}]
        if hard:
            events.insert(0, {"type": "Warning", "reason": "FailedScheduling", "object": f"pod/{svc}-old",
                              "message": "0/3 nodes available: 1 node(s) had untolerated taint (resolved by autoscaler)", "time": _ts(rng, 1)})
        fix = {"action": "set_image", "target": f"deployment/{svc}", "params": {"image": f"{bad.split(':')[0]}:<last-known-good>"}}
        alert = f"KubePodNotReady: {ns}/{pod} ImagePullBackOff"

    elif cat == "missing_secret":
        sec = f"{svc}-creds-v{rng.randint(2, 4)}"
        dep["env_from_secrets"] = [sec]
        P.update(status="CreateContainerConfigError", ready="0/1", restarts=0)
        logs[pod] = []
        events += [{"type": "Warning", "reason": "Failed", "object": f"pod/{pod}",
                    "message": f"Error: secret \"{sec}\" not found", "time": _ts(rng, 2)}]
        if hard:
            events.append({"type": "Warning", "reason": "FailedMount", "object": f"pod/{pod}",
                           "message": "MountVolume.SetUp failed for volume 'tmp': retrying", "time": _ts(rng, 2)})
        fix = {"action": "create_or_rename_secret", "target": f"secret/{sec}", "params": {"namespace": ns, "name": sec}}
        alert = f"KubePodNotReady: {ns}/{pod} CreateContainerConfigError"

    elif cat == "bad_config_value":
        key, badv, why = rng.choice([("PORT", "eighty", "invalid literal for int() with base 10: 'eighty'"),
                                     ("TIMEOUT_MS", "3s", "invalid duration '3s': expected integer milliseconds"),
                                     ("FEATURE_X", "yes please", "cannot parse boolean from 'yes please'")])
        cm[dep["configmap"]][key] = badv
        P.update(status="CrashLoopBackOff", restarts=rng.randint(4, 9))
        logs[pod] = [f"INFO  {svc}: loading config from configmap {dep['configmap']}",
                     f"ERROR {svc}: config key {key}: {why}",
                     f"FATAL {svc}: aborting startup"]
        if hard:
            logs[pod].insert(1, f"WARN  {svc}: env var override check skipped")
        events += [{"type": "Warning", "reason": "BackOff", "object": f"pod/{pod}",
                    "message": "Back-off restarting failed container", "time": _ts(rng, 3)}]
        metrics["PodRestartCount"] = _series(rng, 0, 0.1, ramp=0.6)
        good = {"PORT": "8080", "TIMEOUT_MS": "3000", "FEATURE_X": "true"}[key]
        fix = {"action": "patch_configmap", "target": f"configmap/{dep['configmap']}", "params": {"key": key, "value": good}}
        alert = f"KubePodCrashLooping: {ns}/{pod} restarting ({P['restarts']} in 15m)"

    elif cat == "probe_misconfigured":
        bad_path = rng.choice(["/healthz", "/ready", "/status", "/api/health"])
        dep["readiness_probe"] = {"path": bad_path, "port": 8080, "initial_delay_s": 5}
        P.update(status="Running", ready="0/1", restarts=0)
        logs[pod] = [f"INFO  {svc}: listening on :8080", f"INFO  GET /health 200 1ms",
                     f"INFO  GET {bad_path} 404 0ms", f"INFO  GET {bad_path} 404 0ms"]
        events += [{"type": "Warning", "reason": "Unhealthy", "object": f"pod/{pod}",
                    "message": f"Readiness probe failed: HTTP probe failed with statuscode: 404", "time": _ts(rng, 3)}]
        if hard:
            logs[pod].insert(1, f"WARN  {svc}: upstream timeout after 3000ms (non-fatal)")
        fix = {"action": "patch_readiness_probe", "target": f"deployment/{svc}", "params": {"path": "/health"}}
        alert = f"KubeDeploymentReplicasMismatch: {ns}/{svc} 0/2 ready"

    elif cat == "insufficient_resources":
        req = rng.choice(["8Gi", "16Gi", "24Gi"])
        dep["resources"]["requests"]["memory"] = req
        P.update(status="Pending", ready="0/1", restarts=0)
        logs[pod] = []
        events += [{"type": "Warning", "reason": "FailedScheduling", "object": f"pod/{pod}",
                    "message": "0/3 nodes are available: 3 Insufficient memory. preemption: 0/3 nodes are available: 3 No preemption victims found.",
                    "time": _ts(rng, 2)}]
        metrics["MemoryUtilization"] = _series(rng, 82, 2)  # cluster is busy
        if hard:
            events.append({"type": "Warning", "reason": "FailedScheduling", "object": f"pod/{svc}-batch",
                           "message": "0/3 nodes are available: 1 node(s) had untolerated taint", "time": _ts(rng, 2)})
        fix = {"action": "reduce_memory_request", "target": f"deployment/{svc}", "params": {"memory": "512Mi"}}
        alert = f"KubePodNotReady: {ns}/{pod} Pending for >10m"

    elif cat == "dependency_down":
        d, port = DEPS[(variant + 2) % len(DEPS)]
        dpod = f"{d}-0"
        pods.append({"name": dpod, "ready": "0/1", "status": "CrashLoopBackOff", "restarts": rng.randint(8, 20), "age": "3d"})
        logs[dpod] = [f"INFO  {d}: starting", f"ERROR {d}: no space left on device writing /data/appendonly.aof",
                      f"FATAL {d}: exiting"]
        P.update(status="Running" if not hard else "CrashLoopBackOff", ready="0/1", restarts=rng.randint(0, 6) if hard else 0)
        logs[pod] = [f"INFO  {svc}: starting",
                     f"ERROR {svc}: dial tcp {d}:{port}: connect: connection refused",
                     f"ERROR {svc}: readiness check failed: {d} unreachable"]
        if hard:
            logs[pod].append(f"FATAL {svc}: giving up after 5 retries")
        events += [{"type": "Warning", "reason": "Unhealthy", "object": f"pod/{pod}",
                    "message": "Readiness probe failed: HTTP probe failed with statuscode: 503", "time": _ts(rng, 3)},
                   {"type": "Warning", "reason": "BackOff", "object": f"pod/{dpod}",
                    "message": "Back-off restarting failed container", "time": _ts(rng, 3)}]
        fix = {"action": "expand_pvc", "target": f"statefulset/{d}", "params": {"pvc": f"data-{d}-0", "size": "+20Gi"}}
        alert = f"KubeDeploymentReplicasMismatch: {ns}/{svc} 0/2 ready"
    else:
        raise ValueError(cat)

    # unrelated noise
    logs[pod] = _noise_logs(rng, svc, 1)[:1] + logs[pod] if logs[pod] and rng.random() < 0.5 else logs[pod]
    return Scenario(id=sid, category=cat, namespace=ns, service=svc, alert=alert, pods=pods, logs=logs,
                    events=events, metrics=metrics, deployment=dep, configmaps=cm, truth_fix=fix, hard=hard)


def all_scenarios(seed: int = 7) -> list[Scenario]:
    return [build(c, v, seed) for c in CATEGORIES for v in range(5)]


def get(scenario_id: str, seed: int = 7) -> Scenario:
    for s in all_scenarios(seed):
        if s.id == scenario_id:
            return copy.deepcopy(s)
    raise KeyError(scenario_id)
