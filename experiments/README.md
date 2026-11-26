# Experiments (Member 2): runner script and results

`run_experiments.py` runs every **setting × pattern × repeat** and saves one folder per run.

- Settings: `hpa30`, `hpa50`, `hpa70`, `custom15`, `custom20`
- Patterns (from `load/load-test.js`): `ramp`, `spike`, `periodic`
- 3 repeats → **45 runs, about 8–9 hours**

Each run: delete any HPA → reset `web` to 1 pod and wait 90 s → start the autoscaler →
start `pod_logger.py` → run k6 inside the cluster → wait 30 s → stop everything.

## Before starting

1. App deployed (README steps 1–2), `metrics-server` enabled.
2. Prometheus installed (Member 3) and showing `http_requests_total`.
3. Python setup from `autoscaler/README.md`, and `source ~/venv/bin/activate`.

The script starts and restarts the port-forward to Prometheus by itself
(`--prom-namespace monitoring --prom-service prometheus-operated`; change if yours differ).

## Run

    python3 experiments/run_experiments.py --dry-run          # show the 45 runs
    python3 experiments/run_experiments.py --settings hpa50 custom20 --patterns spike --repeats 1   # quick test first
    nohup python3 experiments/run_experiments.py > experiments/run.log 2>&1 &   # full set, overnight
    tail -f experiments/run.log

`nohup … &` keeps it running if the SSH session closes. Finished runs (with `meta.json`)
are skipped, so after a crash just start the same command again.

## Output: `results/<setting>_<pattern>_r<repeat>/`

| File | Content |
|---|---|
| `pods.csv` | Every 5 s: desired, ready and running pods, pods per node, HPA CPU % |
| `autoscaler.csv` | Custom runs only: every decision (requests/s, desired pods, action, reason) |
| `k6_output.txt` | Full k6 output |
| `k6_summary.json` | avg / p95 / max response time (ms), failed %, requests, requests/s, dropped iterations |
| `meta.json` | Setting, pattern, repeat, start and end time, and the summary |
