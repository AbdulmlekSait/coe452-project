#!/usr/bin/env python3
"""Run every autoscaler setting x traffic pattern x repeat and save the results.

For each run it:
  1. removes any HPA / custom autoscaler and resets `web` to 1 pod,
  2. starts the autoscaler under test (HPA YAML or custom_autoscaler.py),
  3. starts pod_logger.py,
  4. runs the k6 pattern as a pod inside the cluster,
  5. saves the results in the format Member 3's chart script reads
     (docs/charts/README.md) into experiments/results/<kind>-<target>_<pattern>_r<repeat>/,
  6. stops everything and waits for the cluster to settle.

A run folder that already has meta.json is skipped, so the script can be
started again after an interruption and continues where it stopped.

Settings are written <kind>-<target>: hpa-<CPU %> or custom-<requests/s per pod>.
For an HPA target without a YAML file in autoscaler/, the file is made from
hpa-cpu50.yaml with the new target (and saved in the run folder).

Examples:
  python3 experiments/run_experiments.py                       # all 45 runs
  python3 experiments/run_experiments.py --settings hpa-50 custom-20 --patterns spike --repeats 1
  python3 experiments/run_experiments.py --dry-run             # print the plan only
"""
import argparse
import csv
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AUTOSCALER = ROOT / "autoscaler"
LOAD_TEST = ROOT / "load" / "load-test.js"
RESULTS = ROOT / "experiments" / "results"

DEFAULT_SETTINGS = ["hpa-30", "hpa-50", "hpa-70", "custom-15", "custom-20"]
PATTERNS = ["ramp", "spike", "periodic"]
MARKER = "@@K6_SUMMARY_JSON@@"   # same marker Member 3's chart script looks for

# Added to the k6 script only if load-test.js has no handleSummary of its own.
# It prints k6's full JSON summary on one line (k6 runs in a pod that is deleted
# afterwards, so stdout is the way out) plus the end time, used to align pods.csv.
HANDLE_SUMMARY = """
export function handleSummary(data) {
  data.runner_end_ms = Date.now();
  return { stdout: '\\n""" + MARKER + """' + JSON.stringify(data) + '\\n' };
}
"""


def setting_arg(s):
    m = re.fullmatch(r"(hpa|custom)-(\d+(?:\.\d+)?)", s)
    if not m:
        raise argparse.ArgumentTypeError(f"{s}: use hpa-<CPU %> or custom-<req/s>, e.g. hpa-50, custom-20")
    return s


def parse_args():
    p = argparse.ArgumentParser(description="Run the autoscaling experiments")
    p.add_argument("--settings", nargs="+", type=setting_arg, default=DEFAULT_SETTINGS)
    p.add_argument("--patterns", nargs="+", default=PATTERNS, choices=PATTERNS)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--settle", type=int, default=90,
                   help="seconds to wait at 1 pod before each run (default: %(default)s)")
    p.add_argument("--prom-url", default="http://localhost:9090")
    p.add_argument("--prom-namespace", default="monitoring",
                   help="namespace of Prometheus, for the automatic port-forward")
    p.add_argument("--prom-service", default="monitoring-kube-prometheus-prometheus",
                   help="Prometheus Service name, for the automatic port-forward")
    p.add_argument("--query", default=None, help="requests/s PromQL query for the custom autoscaler")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def sh(*cmd, check=False, **kw):
    return subprocess.run(list(cmd), text=True, capture_output=True, check=check, **kw)


def split_setting(setting):
    kind, target = setting.split("-", 1)
    target = float(target)
    return kind, int(target) if target.is_integer() else target


def hpa_manifest(target):
    """Text of the HPA YAML for a CPU target: the file if it exists, else made from hpa-cpu50.yaml."""
    f = AUTOSCALER / f"hpa-cpu{target}.yaml"
    if f.exists():
        return f.read_text()
    base = (AUTOSCALER / "hpa-cpu50.yaml").read_text()
    return re.sub(r"averageUtilization:\s*\d+", f"averageUtilization: {target}", base)


# ---------- cluster helpers ----------

def reset_cluster():
    """No autoscaler, web at 1 ready pod."""
    sh("kubectl", "delete", "hpa", "web", "--ignore-not-found")
    sh("kubectl", "scale", "deployment", "web", "--replicas=1", check=True)
    sh("kubectl", "rollout", "status", "deployment/web", "--timeout=180s")
    for _ in range(60):          # wait until the extra pods have really gone
        out = sh("kubectl", "get", "pods", "-l", "app=web", "--no-headers").stdout.strip()
        if len(out.splitlines()) <= 1:
            return
        time.sleep(5)


class PortForward:
    """Keeps `kubectl port-forward` to Prometheus alive (it can drop overnight)."""

    def __init__(self, args):
        self.args, self.proc = args, None

    def query(self, q):
        url = f"{self.args.prom_url}/api/v1/query?" + urllib.parse.urlencode({"query": q})
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.load(r)["data"]["result"]

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

    def app_scraped(self):
        """True if Prometheus scrapes at least one web pod (else the custom autoscaler would see 0 req/s)."""
        try:
            return any(r["value"][1] == "1" for r in self.query('up{job="web"}'))
        except Exception:
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

def k6_script():
    text = LOAD_TEST.read_text()
    return text if "handleSummary" in text else text + HANDLE_SUMMARY


def extract_k6_json(text):
    if MARKER not in text:
        return None
    try:
        return json.loads(text.split(MARKER, 1)[1].strip().splitlines()[0])
    except (json.JSONDecodeError, IndexError):
        return None


def summarize(data):
    """Main numbers from k6's JSON summary (handleSummary format)."""
    m = data.get("metrics", {})

    def vals(name):
        v = m.get(name, {})
        return v.get("values", v)

    dur, failed, reqs = vals("http_req_duration"), vals("http_req_failed"), vals("http_reqs")
    r = lambda x: None if x is None else round(float(x), 2)   # noqa: E731
    return {"latency_avg_ms": r(dur.get("avg")), "latency_p95_ms": r(dur.get("p(95)")),
            "latency_max_ms": r(dur.get("max")),
            "failed_percent": r(100 * float(failed.get("rate", 0.0))),
            "requests": reqs.get("count"), "requests_per_s": r(reqs.get("rate")),
            "dropped_iterations": vals("dropped_iterations").get("count", 0)}


def run_k6(pattern, name, out_file):
    sh("kubectl", "delete", "pod", name, "--ignore-not-found")
    with open(out_file, "w") as out:
        proc = subprocess.run(
            ["kubectl", "run", name, "--rm", "-i", "--restart=Never", "--image=grafana/k6",
             "--", "run", "-q", "-e", f"PATTERN={pattern}", "-"],
            input=k6_script(), stdout=out, stderr=subprocess.STDOUT, text=True, timeout=1800)
    return proc.returncode


def k6_start_epoch(data, returned_at, pod_started_at):
    """When the k6 test itself started (seconds since 1970), to give pods.csv its t column."""
    dur_ms = (data or {}).get("state", {}).get("testRunDurationMs")
    if data and dur_ms and data.get("runner_end_ms"):
        return (data["runner_end_ms"] - dur_ms) / 1000, "k6 end time - test duration"
    if dur_ms:
        return returned_at - dur_ms / 1000, "kubectl return - test duration (approx.)"
    return pod_started_at, "k6 pod start (approx.)"


def add_t_column(raw_csv, out_csv, t0, first_cols):
    """Copy a CSV that has an epoch column, adding t = seconds since the k6 test started."""
    if not raw_csv.exists():
        return
    with open(raw_csv, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return
    rest = [c for c in rows[0] if c not in first_cols and c != "t"]
    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["t"] + first_cols + rest)
        w.writeheader()
        for row in rows:
            try:
                row["t"] = round(float(row["epoch"]) - t0, 1)
            except (KeyError, ValueError):
                continue
            w.writerow(row)


# ---------- one run ----------

def one_run(args, setting, pattern, repeat, pf):
    kind, target = split_setting(setting)
    run_id = f"{setting}_{pattern}_r{repeat}"
    out = RESULTS / run_id
    if (out / "meta.json").exists():
        log(f"skip {run_id} (already done)")
        return
    out.mkdir(parents=True, exist_ok=True)

    log(f"=== {run_id}: reset to 1 pod")
    reset_cluster()
    time.sleep(args.settle)

    autoscaler = None
    if kind == "hpa":
        manifest = hpa_manifest(target)
        (out / "hpa.yaml").write_text(manifest)
        sh("kubectl", "apply", "-f", "-", input=manifest, check=True)
    else:
        if not pf.ensure():
            log("ERROR: Prometheus not reachable, skipping this run")
            return
        if not pf.app_scraped():
            log('ERROR: Prometheus does not scrape the web app (up{job="web"} empty). '
                "Check: kubectl get svc web --show-labels  (needs app=web). Skipping this run")
            return
        cmd = [sys.executable, str(AUTOSCALER / "custom_autoscaler.py"),
               "--target", str(target), "--prom-url", args.prom_url,
               "--csv", str(out / "autoscaler_raw.csv")]
        if args.query:
            cmd += ["--query", args.query]
        autoscaler = subprocess.Popen(cmd, stdout=open(out / "autoscaler.log", "w"),
                                      stderr=subprocess.STDOUT)

    logger = subprocess.Popen([sys.executable, str(AUTOSCALER / "pod_logger.py"),
                               "--csv", str(out / "pods_raw.csv")],
                              stdout=subprocess.DEVNULL, stderr=open(out / "pod_logger.log", "w"))

    time.sleep(10)        # a few pod samples before the test starts (negative t)
    started = time.time()
    log(f"k6 {pattern} running ...")
    rc = run_k6(pattern, f"k6-{run_id.replace('_', '-').replace('.', '-')}", out / "k6_output.txt")
    returned = time.time()

    # keep logging a little after the load ends, to see the scale-down start
    time.sleep(30)
    stop_proc(autoscaler)
    stop_proc(logger)
    sh("kubectl", "delete", "hpa", "web", "--ignore-not-found")

    data = extract_k6_json((out / "k6_output.txt").read_text(errors="replace"))
    if data:
        (out / "k6-summary.json").write_text(json.dumps(data))
    summary = summarize(data) if data else {"error": "no k6 JSON summary in k6_output.txt"}
    t0, t0_method = k6_start_epoch(data, returned, started)
    add_t_column(out / "pods_raw.csv", out / "pods.csv", t0, ["desired", "ready"])
    add_t_column(out / "autoscaler_raw.csv", out / "autoscaler.csv", t0, ["rps", "current_pods", "new_pods"])

    meta = {"kind": kind, "target": target, "pattern": pattern, "repeat": repeat,
            "run": run_id, "per_pod_target": "CPU % of request" if kind == "hpa" else "requests/s",
            "k6_start_utc": datetime.fromtimestamp(t0, timezone.utc).isoformat(timespec="seconds"),
            "k6_start_method": t0_method,
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
