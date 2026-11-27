#!/usr/bin/env python3
"""Record the number of web pods every few seconds to a CSV file.

Columns: desired = spec.replicas, ready = status.readyReplicas, running pods, how the running
pods are spread over the nodes, and (when an HPA exists) its current CPU
utilization and desired replicas.

Example:
  python3 pod_logger.py --csv pods.csv
"""
import argparse
import csv
import signal
import time
from collections import Counter
from datetime import datetime, timezone

from kubernetes import client, config

CSV_FIELDS = ["timestamp", "epoch", "elapsed_s", "desired", "ready", "running_pods",
              "pods_per_node", "hpa_cpu_percent", "hpa_desired"]


def parse_args():
    p = argparse.ArgumentParser(description="Log the number of pods of a Deployment")
    p.add_argument("--deployment", default="web")
    p.add_argument("--namespace", default="default")
    p.add_argument("--selector", default="app=web", help="label selector of the pods")
    p.add_argument("--interval", type=float, default=5)
    p.add_argument("--csv", default="pods.csv")
    return p.parse_args()


def hpa_status(autoscaling, name, namespace):
    """Current CPU % and desired replicas of the HPA, or blanks if there is none."""
    try:
        h = autoscaling.read_namespaced_horizontal_pod_autoscaler(name, namespace)
    except client.exceptions.ApiException:
        return "", ""
    cpu = ""
    for m in h.status.current_metrics or []:
        if m.resource and m.resource.name == "cpu":
            cpu = m.resource.current.average_utilization
    return cpu, h.status.desired_replicas


def main():
    args = parse_args()
    try:
        config.load_kube_config()
    except Exception:
        config.load_incluster_config()
    apps, core = client.AppsV1Api(), client.CoreV1Api()
    autoscaling = client.AutoscalingV2Api()

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
        while running:
            loop_start = time.monotonic()
            row = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "epoch": round(time.time(), 2), "elapsed_s": round(loop_start - start, 1)}
            try:
                dep = apps.read_namespaced_deployment(args.deployment, args.namespace)
                pods = core.list_namespaced_pod(args.namespace, label_selector=args.selector).items
                running_pods = [p for p in pods
                                if p.status.phase == "Running" and not p.metadata.deletion_timestamp]
                per_node = Counter(p.spec.node_name for p in running_pods)
                cpu, hpa_desired = hpa_status(autoscaling, args.deployment, args.namespace)
                row.update(desired=dep.spec.replicas,
                           ready=dep.status.ready_replicas or 0,
                           running_pods=len(running_pods),
                           pods_per_node=";".join(f"{n}:{c}" for n, c in sorted(per_node.items())),
                           hpa_cpu_percent=cpu, hpa_desired=hpa_desired)
            except Exception as e:
                row.update(pods_per_node=f"error: {str(e)[:150]}")
            writer.writerow(row)
            f.flush()
            while running and time.monotonic() - loop_start < args.interval:
                time.sleep(0.2)


if __name__ == "__main__":
    main()
