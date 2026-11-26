#!/usr/bin/env python3
"""Run every autoscaler setting x traffic pattern x repeat and save the results.

For each run it:
  1. removes any HPA / custom autoscaler and resets `web` to 1 pod,
  2. starts the autoscaler under test (HPA YAML or custom_autoscaler.py),
  3. starts pod_logger.py,
  4. runs the k6 pattern as a pod inside the cluster,
  5. saves k6 output, a parsed summary, both CSV files and meta.json into
     experiments/results/<setting>_<pattern>_r<repeat>/,
  6. stops everything and waits for the cluster to settle.

A run folder that already has meta.json is skipped, so the script can be
started again after an interruption and continues where it stopped.

Examples:
  python3 experiments/run_experiments.py                       # all 45 runs
  python3 experiments/run_experiments.py --settings hpa50 custom20 --patterns spike --repeats 1
  python3 experiments/run_experiments.py --dry-run             # print the plan only
"""
import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AUTOSCALER = ROOT / "autoscaler"
LOAD_TEST = ROOT / "load" / "load-test.js"
RESULTS = ROOT / "experiments" / "results"

SETTINGS = {
    "hpa30": {"kind": "hpa", "file": AUTOSCALER / "hpa-cpu30.yaml"},
    "hpa50": {"kind": "hpa", "file": AUTOSCALER / "hpa-cpu50.yaml"},
    "hpa70": {"kind": "hpa", "file": AUTOSCALER / "hpa-cpu70.yaml"},
    "custom15": {"kind": "custom", "target": 15},
    "custom20": {"kind": "custom", "target": 20},
}
PATTERNS = ["ramp", "spike", "periodic"]


def parse_args():
    p = argparse.ArgumentParser(description="Run the autoscaling experiments")
    p.add_argument("--settings", nargs="+", default=list(SETTINGS), choices=list(SETTINGS))
    p.add_argument("--patterns", nargs="+", default=PATTERNS, choices=PATTERNS)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--settle", type=int, default=90,
                   help="seconds to wait at 1 pod before each run (default: %(default)s)")
    p.add_argument("--prom-url", default="http://localhost:9090")
    p.add_argument("--prom-namespace", default="monitoring",
                   help="namespace of Prometheus, for the automatic port-forward")
    p.add_argument("--prom-service", default="prometheus-operated",
                   help="Prometheus Service name, for the automatic port-forward")
    p.add_argument("--query", default=None, help="requests/s PromQL query for the custom autoscaler")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def sh(*cmd, check=False, **kw):
    return subprocess.run(list(cmd), text=True, capture_output=True, check=check, **kw)


# ---------- cluster helpers ----------

def reset_cluster():
    """No autoscaler, web at 1 ready pod."""
    sh("kubectl", "delete", "hpa", "web", "--ignore-not-found")
    sh("kubectl", "scale", "deployment", "web", "--replicas=1", check=True)
    sh("kubectl", "rollout", "status", "deployment/web", "--timeout=180s")
    # wait until the extra pods have really gone
    for _ in range(60):
        out = sh("kubectl", "get", "pods", "-l", "app=web", "--no-headers").stdout.strip()
        if len(out.splitlines()) <= 1:
            return
        time.sleep(5)


class PortForward:
    """Keeps `kubectl port-forward` to Prometheus alive (it can drop overnight)."""

    def __init__(self, args):
        self.args, self.proc = args, None

    def prom_ok(self):
        try:
            with urllib.request.urlopen(f"{self.args.prom_url}/-/ready", timeout=3) as r:
                return r.status == 200
        except Exception:
            return False

    def ensure(self):
        if self.prom_ok():
            return True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        port = self.args.prom_url.rsplit(":", 1)[-1].strip("/")
        log(f"starting port-forward to {self.args.prom_namespace}/{self.args.prom_service}:9090")
        self.proc = subprocess.Popen(
            ["kubectl", "port-forward", "-n", self.args.prom_namespace,
             f"svc/{self.args.prom_service}", f"{port}:9090"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(15):
            time.sleep(1)
            if self.prom_ok():
                return True
        return False

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


def stop_proc(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------- k6 ----------

def to_ms(v):
    m = re.match(r"([\d.]+)(µs|us|ms|s|m)?", v)
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2) or "ms"
    return round(n * {"µs": 0.001, "us": 0.001, "ms": 1, "s": 1000, "m": 60000}[unit], 2)


def parse_k6(text):
    """Pull the main numbers out of k6's text summary."""
    s = {}
    m = re.search(r"http_req_duration\W*:?\s*avg=(\S+)\s+p\(95\)=(\S+)\s+max=(\S+)", text)
    if m:
        s["latency_avg_ms"], s["latency_p95_ms"], s["latency_max_ms"] = map(to_ms, m.groups())
    m = re.search(r"http_req_failed\W*:?\s*([\d.]+)%", text)
    if m:
        s["failed_percent"] = float(m.group(1))
    m = re.search(r"http_reqs\W*:?\s*(\d+)\s+([\d.]+)/s", text)
    if m:
        s["requests"], s["requests_per_s"] = int(m.group(1)), float(m.group(2))
    m = re.search(r"dropped_iterations\W*:?\s*(\d+)", text)
    s["dropped_iterations"] = int(m.group(1)) if m else 0
    return s


def run_k6(pattern, name, out_file):
    sh("kubectl", "delete", "pod", name, "--ignore-not-found")
    with open(LOAD_TEST) as script, open(out_file, "w") as out:
        proc = subprocess.run(
            ["kubectl", "run", name, "--rm", "-i", "--restart=Never", "--image=grafana/k6",
             "--", "run", "-q", "-e", f"PATTERN={pattern}", "-"],
            stdin=script, stdout=out, stderr=subprocess.STDOUT, text=True, timeout=1800)
    return proc.returncode


# ---------- one run ----------

def one_run(args, setting, pattern, repeat, pf):
    run_id = f"{setting}_{pattern}_r{repeat}"
    out = RESULTS / run_id
    if (out / "meta.json").exists():
        log(f"skip {run_id} (already done)")
        return
    out.mkdir(parents=True, exist_ok=True)
    cfg = SETTINGS[setting]

    log(f"=== {run_id}: reset to 1 pod")
    reset_cluster()
    time.sleep(args.settle)

    autoscaler = None
    if cfg["kind"] == "hpa":
        sh("kubectl", "apply", "-f", str(cfg["file"]), check=True)
    else:
        if not pf.ensure():
            log("ERROR: Prometheus not reachable, skipping this run")
            return
        cmd = [sys.executable, str(AUTOSCALER / "custom_autoscaler.py"),
               "--target", str(cfg["target"]), "--prom-url", args.prom_url,
               "--csv", str(out / "autoscaler.csv")]
        if args.query:
            cmd += ["--query", args.query]
        autoscaler = subprocess.Popen(cmd, stdout=open(out / "autoscaler.log", "w"),
                                      stderr=subprocess.STDOUT)

    logger = subprocess.Popen([sys.executable, str(AUTOSCALER / "pod_logger.py"),
                               "--csv", str(out / "pods.csv")],
                              stdout=subprocess.DEVNULL, stderr=open(out / "pod_logger.log", "w"))

    started = datetime.now(timezone.utc)
    log(f"k6 {pattern} running ...")
    rc = run_k6(pattern, f"k6-{run_id.replace('_', '-')}", out / "k6_output.txt")
    ended = datetime.now(timezone.utc)

    # keep logging a little after the load ends, to see the scale-down start
    time.sleep(30)
    stop_proc(autoscaler)
    stop_proc(logger)
    sh("kubectl", "delete", "hpa", "web", "--ignore-not-found")

    summary = parse_k6((out / "k6_output.txt").read_text(errors="replace"))
    (out / "k6_summary.json").write_text(json.dumps(summary, indent=2))
    meta = {"run": run_id, "setting": setting, "pattern": pattern, "repeat": repeat,
            "autoscaler": cfg["kind"], "target": cfg.get("target"),
            "hpa_file": cfg["file"].name if cfg["kind"] == "hpa" else None,
            "start_utc": started.isoformat(timespec="seconds"),
            "end_utc": ended.isoformat(timespec="seconds"),
            "k6_exit_code": rc, "summary": summary}
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    log(f"done {run_id}: {summary}")


def main():
    args = parse_args()
    plan = [(s, p, r) for r in range(1, args.repeats + 1)
            for s in args.settings for p in args.patterns]
    log(f"{len(plan)} runs planned")
    if args.dry_run:
        for s, p, r in plan:
            print(f"  {s}_{p}_r{r}")
        return

    RESULTS.mkdir(parents=True, exist_ok=True)
    pf = PortForward(args)
    try:
        for s, p, r in plan:
            try:
                one_run(args, s, p, r, pf)
            except Exception as e:      # one failed run should not stop the night
                log(f"ERROR in {s}_{p}_r{r}: {e}")
    finally:
        pf.stop()
        reset_cluster()
    log("all runs finished")


if __name__ == "__main__":
    main()
