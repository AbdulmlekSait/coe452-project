#!/usr/bin/env python3
"""Custom request-based autoscaler for the `web` Deployment.

Every INTERVAL seconds:
  1. read the total requests per second (RPS) from Prometheus,
  2. desired pods = ceil(RPS / per-pod target - 0.1), kept between MIN and MAX
     (the 0.1 tolerance, like the HPA's, ignores small overshoots),
  3. scale up at once (after a short up-cooldown),
     scale down only when every desired value over the last DOWN_WINDOW
     seconds is lower (stabilization, so short dips do not remove pods),
  4. log the decision to a CSV file.

Example:
  python3 custom_autoscaler.py --target 20 --csv decisions.csv
"""
import argparse
import csv
import math
import signal
import sys
import time
from collections import deque
from datetime import datetime, timezone

import requests
from kubernetes import client, config

# Member 3's query: /work requests only, 30 s average (app scraped every 5 s)
DEFAULT_QUERY = 'sum(rate(http_requests_total{job="web",handler="/work"}[30s]))'
# Used to tell "no traffic" apart from "Prometheus is not scraping the app"
UP_QUERY = 'up{job="web"}'

CSV_FIELDS = ["timestamp", "epoch", "elapsed_s", "rps", "current_pods", "raw_desired",
              "desired", "new_pods", "action", "reason"]


def parse_args():
    p = argparse.ArgumentParser(description="Request-based autoscaler for a Deployment")
    p.add_argument("--target", type=float, required=True,
                   help="requests per second one pod should handle (e.g. 15 or 20)")
    p.add_argument("--prom-url", default="http://localhost:9090",
                   help="Prometheus base URL (default: %(default)s)")
    p.add_argument("--query", default=DEFAULT_QUERY,
                   help="PromQL query returning total requests/s (default: %(default)s)")
    p.add_argument("--deployment", default="web")
    p.add_argument("--namespace", default="default")
    p.add_argument("--min", dest="min_pods", type=int, default=1)
    p.add_argument("--max", dest="max_pods", type=int, default=10)
    p.add_argument("--interval", type=float, default=10, help="seconds between decisions")
    p.add_argument("--up-cooldown", type=float, default=20,
                   help="minimum seconds between two scale-ups")
    p.add_argument("--down-window", type=float, default=60,
                   help="desired value must stay lower for this many seconds before scaling down")
    p.add_argument("--tolerance", type=float, default=0.1,
                   help="ignore this fraction of a pod when rounding up (default: %(default)s, like the HPA)")
    p.add_argument("--csv", default="autoscaler_decisions.csv", help="decision log file")
    p.add_argument("--dry-run", action="store_true", help="decide and log, but do not scale")
    return p.parse_args()


def desired_pods(rps, target, min_pods, max_pods, tolerance=0.1):
    """pods = ceil(rps / target - tolerance), kept between min_pods and max_pods.

    The tolerance (like the HPA's default 10%) stops a reading just above a multiple
    of the target (e.g. 40.04 req/s with target 20) from adding a whole extra pod."""
    raw = max(0, math.ceil(rps / target - tolerance)) if rps > 0 else 0
    return raw, max(min_pods, min(max_pods, raw))


class Decider:
    """Turns raw desired values into scaling actions with cooldown and stabilization."""

    def __init__(self, up_cooldown, down_window):
        self.up_cooldown = up_cooldown
        self.down_window = down_window
        self.history = deque()          # (time, desired) of the last down_window seconds
        self.started = None
        self.last_scale_up = -math.inf

    def decide(self, now, current, desired):
        if self.started is None:
            self.started = now
        self.history.append((now, desired))
        while self.history[0][0] < now - self.down_window:
            self.history.popleft()

        if desired > current:
            if now - self.last_scale_up < self.up_cooldown:
                return current, "hold", "scale-up cooldown"
            self.last_scale_up = now
            return desired, "scale_up", f"rps needs {desired} pods"

        if desired < current:
            if now - self.started < self.down_window:
                return current, "hold", "waiting for down window"
            # highest value seen in the last down_window seconds: only a
            # sustained drop removes pods
            stable = max(d for _, d in self.history)
            if stable < current:
                return stable, "scale_down", f"lower for {self.down_window:.0f}s"
            return current, "hold", "load not low for whole window"

        return current, "hold", "at desired"


def prom_query(prom_url, query):
    r = requests.get(f"{prom_url}/api/v1/query", params={"query": query}, timeout=5)
    r.raise_for_status()
    return r.json()["data"]["result"]


def query_rps(prom_url, query):
    """Total requests/s. Raises an error (so the autoscaler keeps the current pods) when
    Prometheus is down or is not scraping the app at all."""
    result = prom_query(prom_url, query)
    if not result:
        # Empty can mean "no traffic" or "the app is not scraped" (e.g. the web Service
        # lost its app=web label). Only the first is a real 0.
        if not prom_query(prom_url, UP_QUERY):
            raise RuntimeError('Prometheus has no up{job="web"} target: app not scraped')
        return 0.0
    value = float(result[0]["value"][1])
    return 0.0 if math.isnan(value) else value


def main():
    args = parse_args()
    try:
        config.load_kube_config()
    except Exception:
        config.load_incluster_config()
    apps = client.AppsV1Api()

    decider = Decider(args.up_cooldown, args.down_window)
    running = True

    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    start = time.monotonic()
    with open(args.csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        print(f"autoscaler: target={args.target} req/s per pod, "
              f"{args.min_pods}-{args.max_pods} pods, every {args.interval:.0f}s", flush=True)

        while running:
            loop_start = time.monotonic()
            now = loop_start - start
            row = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "epoch": round(time.time(), 2), "elapsed_s": round(now, 1)}
            try:
                scale = apps.read_namespaced_deployment_scale(args.deployment, args.namespace)
                current = scale.spec.replicas
                rps = query_rps(args.prom_url, args.query)
                raw, desired = desired_pods(rps, args.target, args.min_pods, args.max_pods, args.tolerance)
                new, action, reason = decider.decide(now, current, desired)
                if new != current and not args.dry_run:
                    apps.patch_namespaced_deployment_scale(
                        args.deployment, args.namespace, {"spec": {"replicas": new}})
                row.update(rps=round(rps, 2), current_pods=current, raw_desired=raw,
                           desired=desired, new_pods=new, action=action, reason=reason)
            except Exception as e:      # keep running if Prometheus or the API is briefly down
                row.update(action="error", reason=str(e)[:200])
            writer.writerow(row)
            f.flush()
            print(row, flush=True)

            while running and time.monotonic() - loop_start < args.interval:
                time.sleep(0.2)
    print("autoscaler stopped", flush=True)


if __name__ == "__main__":
    sys.exit(main())
