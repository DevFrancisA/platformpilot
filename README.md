# PlatformPilot

An agentic assistant that investigates Kubernetes alerts. It queries pod logs, events and CloudWatch metrics,
proposes a root cause and a fix, and **waits for an engineer to approve** before anything is applied.

Everything runs locally: clone, install, open the UI. No cloud account or API key required.

```bash
pip install -r requirements.txt
python -m platformpilot          # UI at http://127.0.0.1:8000
python -m platformpilot eval     # headless evaluation over all 40 scenarios
pytest -q
```

- **No key:** a deterministic planner drives the same tools (good for demos/CI).
- **With `ANTHROPIC_API_KEY`:** Claude runs the investigation via tool use (`PLATFORMPILOT_MODEL` to override).
  Force with `--mode llm|offline`. Run `python -m platformpilot eval --mode llm` for the real LLM number.

## What's here

| Path | Purpose |
|---|---|
| `platformpilot/scenarios.py` | 40 injected failures: 8 classes x 5 variants (2 per class are noisy/misleading) |
| `platformpilot/tools.py` | Read-only tools (`list_pods`, `get_events`, `get_pod_logs`, `describe_pod`, `get_deployment_config`, `get_cloudwatch_metrics`), gated `apply_fix`, plus a `LiveCluster` (kubectl + boto3) |
| `platformpilot/agent.py` | LLM agent + offline agent; both emit `{category, summary, evidence, fix, confidence}` |
| `platformpilot/runner.py` | Runs, approval flow, evaluation, Prometheus metrics |
| `platformpilot/static/index.html` | Local UI (live trace, approve/reject, eval dashboard) |
| `terraform/` | Test EKS cluster + Container Insights + read-only CloudWatch policy |
| `k8s/failures/` | Real failure manifests to inject on a test cluster |
| `deploy/` | Prometheus + Grafana (`docker compose -f deploy/docker-compose.yml up`, Grafana on :3000) |

Failure classes: missing env var, OOM kill, bad image tag, missing secret, bad ConfigMap value,
misconfigured readiness probe, unschedulable (insufficient resources), dependency down (full disk on Redis/Postgres/...).

## Methodology (read before quoting numbers)

- **Accuracy** = predicted category equals injected ground truth; **fix accuracy** = proposed action equals the correct remediation;
  **remediation success** = the simulated cluster is healthy after the approved fix.
- **Scenarios are simulated** snapshots served through the same tool interface a real cluster would use. The offline planner is
  rule-based and was developed against these scenarios, so its score is an upper bound, not a generalization estimate.
  Use `--mode llm` and the real-cluster path below for numbers you can defend.
- **Manual baseline is modeled, not measured:** 4 min ack/triage + 2.5 min per lookup a human needs for that failure class.
  **Agent time** = measured compute + 1 min assumed human review/approval. Assumptions live in `runner.py` and are printed in the UI.
- Results are written to `eval_results.json` and exported to Grafana via `/metrics`
  (`platformpilot_eval_*`, run counts, latency histogram, tool calls, process CPU/RSS).

## Running against a real cluster

1. `cd terraform && terraform init && terraform apply` (costs money), then run the printed `update-kubeconfig` command.
2. `kubectl apply -f k8s/failures/failures.yaml`
3. Wire `LiveCluster` in `runner.py` (`SimCluster` -> `LiveCluster(namespace, cluster)`). Live `apply_fix` is deliberately a no-op that prints the fix for you to run.
   `LiveCluster` and the Terraform have not been exercised against AWS in this repo's tests.

## Safety

The agent only has read tools. The write path (`apply_fix`) is reachable solely through the approve endpoint.
