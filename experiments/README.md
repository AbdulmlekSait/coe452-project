# Experiments (Member 2): runner script and results

`run_experiments.py` runs every **setting × pattern × repeat** and saves one folder per run.

- Settings: `hpa-30`, `hpa-50`, `hpa-70`, `custom-15`, `custom-20` (any `hpa-<CPU %>` or `custom-<req/s>` works)
- Patterns (from `load/load-test.js`): `ramp`, `spike`, `periodic`
- 3 repeats → **45 runs, about 8–9 hours**

Each run: delete any HPA → reset `web` to 1 pod and wait 90 s → start the autoscaler →
start `pod_logger.py` → run k6 inside the cluster → wait 30 s → stop everything.

## Before starting

1. App deployed (README steps 1–2), `metrics-server` enabled.
2. Prometheus installed (Member 3) and showing `http_requests_total`.
3. Python setup from `autoscaler/README.md`, and `source ~/venv/bin/activate`.

The script starts and restarts the port-forward to Prometheus by itself
(`--prom-namespace monitoring --prom-service monitoring-kube-prometheus-prometheus`, as installed by `monitoring/install.sh`).
Before each custom run it also checks that Prometheus scrapes the app (`up{job="web"}`), and skips the run with an error if not.

## Run

    python3 experiments/run_experiments.py --dry-run          # show the 45 runs
    python3 experiments/run_experiments.py --settings hpa-50 custom-20 --patterns spike --repeats 1   # quick test first
    nohup python3 experiments/run_experiments.py > experiments/run.log 2>&1 &   # full set, overnight
    tail -f experiments/run.log

`nohup … &` keeps it running if the SSH session closes. Finished runs (with `meta.json`)
are skipped, so after a crash just start the same command again.

## Output: `results/<kind>-<target>_<pattern>_r<repeat>/` (e.g. `hpa-50_spike_r1`)

This is the format Member 3's `docs/charts/make_charts.py` reads (see `docs/charts/README.md`).

| File | Content |
|---|---|
| `pods.csv` | Every 5 s: `t` (seconds since k6 started, negative before), `desired` (spec.replicas), `ready` (readyReplicas), running pods, pods per node, HPA CPU % |
| `k6-summary.json` | k6's full JSON summary |
| `meta.json` | kind, target, pattern, repeat, k6 start time, and the main numbers (avg / p95 / max ms, failed %, requests) |
| `autoscaler.csv` | Custom runs only: every decision with `t`, requests/s, current and new pods, action, reason |
| `hpa.yaml` | HPA runs only: the exact HPA that was applied |
| `k6_output.txt`, `*_raw.csv`, `*.log` | Raw output, kept for checking |

The k6 summary is read from k6's output: the runner adds a `handleSummary` to the k6 script
(only if `load/load-test.js` has none) that prints the JSON after `@@K6_SUMMARY_JSON@@`.

Make the charts after the runs: `python3 docs/charts/make_charts.py`
