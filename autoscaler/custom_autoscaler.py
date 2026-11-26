#!/usr/bin/env python3
"""Custom request-based autoscaler for the `web` Deployment.

Every INTERVAL seconds:
  1. read the total requests per second (RPS) from Prometheus,
  2. desired pods = ceil(RPS / per-pod target), kept between MIN and MAX,
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

DEFAULT_QUERY = 'sum(rate(http_requests_total{handler="/work"}[1m]))'

CSV_FIELDS = ["timestamp", "elapsed_s", "rps", "current_pods", "raw_desired",
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
    p.add_argument("--csv", default="autoscaler_decisions.csv", help="decision log file")
    p.add_argument("--dry-run", action="store_true", help="decide and log, but do not scale")
    return p.parse_args()


def desired_pods(rps, target, min_pods, max_pods):
    """pods = ceil(rps / target), kept between min_pods and max_pods."""
    raw = math.ceil(rps / target) if rps > 0 else 0
    return raw, max(min_pods, min(max_pods, raw))


class Decider:
    """Turns raw desired values into scaling actions with cooldown and stabilization."""

    def __init__(self, up_cooldown, down_window):
        self.up_cooldown = up_cooldown
        self.down_window = down_window
        self.history = deque()          # (time, desired)
        self.last_scale_up = -math.inf

    def decide(self, now, current, desired):
        self.history.append((now, desired))
        while self.history and self.history[0][0] < now - self.down_window:
            self.history.popleft()

        if desired > current:
            if now - self.last_scale_up < self.up_cooldown:
                return current, "hold", "scale-up cooldown"
            self.last_scale_up = now
            return desired, "scale_up", f"rps needs {desired} pods"

        if desired < current:
            # use the highest desired value seen in the window, so only a
            # sustained drop removes pods
            window_full = now - self.history[0][0] >= self.down_window - 1e-6
            if not window_full:
                return current, "hold", "waiting for down window"
            stable = max(d for _, d in self.history)
            if stable < current:
                return stable, "scale_down", f"lower for {self.down_window:.0f}s"
            return current, "hold", "load not low for whole window"

        return current, "hold", "at desired"


def query_rps(prom_url, query):
    r = requests.get(f"{prom_url}/api/v1/query", params={"query": query}, timeout=5)
    r.raise_for_status()
    result = r.json()["data"]["result"]
    if not result:            # no traffic yet: Prometheus returns an empty vector
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
                   "elapsed_s": round(now, 1)}
            try:
                scale = apps.read_namespaced_deployment_scale(args.deployment, args.namespace)
                current = scale.spec.replicas
                rps = query_rps(args.prom_url, args.query)
                raw, desired = desired_pods(rps, args.target, args.min_pods, args.max_pods)
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
