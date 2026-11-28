# Monitoring (Prometheus + Grafana) — owner: Member 3

Prometheus collects the app's numbers; Grafana (Member 4) draws them; the custom
autoscaler (Member 2) reads requests/s from Prometheus.

```
web pods (/metrics) --> ServiceMonitor --> Prometheus --+--> requests/s query --> custom autoscaler (Member 2)
                                                        +--> 4 queries --------> Grafana dashboard (Member 4)
```

| File | What it does |
|---|---|
| `install.sh` | Installs kube-prometheus-stack with Helm, then applies the ServiceMonitor |
| `values.yaml` | Trimmed chart settings for minikube (no Alertmanager, 5s app scrape, 2-day history) |
| `servicemonitor.yaml` | Tells Prometheus to scrape `web` `/metrics` every 5s |
| `dashboard.json` | Grafana dashboard (Member 4) |

## 1. Install

Needs the cluster and app from the main README steps 1–2, plus Helm
(`sudo snap install helm --classic`).

```bash
bash monitoring/install.sh
```

The script also runs `kubectl label svc web app=web`, because the ServiceMonitor finds
the Service by that label and `k8s/web.yaml` doesn't set it yet. Once Member 1 adds the
label to `k8s/web.yaml`, that line does nothing and can stay.

## 2. Check that it works

```bash
# a) All monitoring pods Running
kubectl get pods -n monitoring

# b) The app exposes the metrics we expect
kubectl run curl --rm -it --image=curlimages/curl --restart=Never -- \
  sh -c 'curl -s http://web/metrics | grep -E "^http_requests_total|^http_request_duration_highr_seconds_count"'

# c) Prometheus scrapes it (in another terminal keep the port-forward running)
kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090:9090
curl -s http://localhost:9090/api/v1/query --data-urlencode 'query=up{job="web"}'
#    -> one result per web pod, each with value "1".
#    Empty result = not scraped: check `kubectl get svc web --show-labels` shows app=web

# d) The requests/s query matches k6: run a k6 test, and while it runs the value below
#    should be close to the request rate k6 reports (http_reqs per second)
curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=sum(rate(http_requests_total{job="web",handler="/work"}[30s]))'
```

The app must keep running **one** uvicorn worker per pod (the Dockerfile `CMD` does
this now). With several workers, each scrape reaches a random worker and the counts
are wrong.

## 3. The four queries

**1. Requests per second** (sent to Member 2 for the custom autoscaler)
```promql
sum(rate(http_requests_total{job="web", handler="/work"}[30s]))
```
Per pod, for the dashboard:
```promql
sum by (pod) (rate(http_requests_total{job="web", handler="/work"}[30s]))
```
Only `/work` is counted, so readiness checks (`/health`) and Prometheus's own scrapes
(`/metrics`) don't add to the demand. The value is a 30-second average: in a test, a jump
from 10 to 30 req/s showed half the change after 15 s and all of it after 30 s. A shorter
window reacts faster but is noisier.

**2. 95th-percentile response time (seconds)**
```promql
histogram_quantile(0.95, sum by (le) (rate(http_request_duration_highr_seconds_bucket{job="web"}[1m])))
```
Why this metric: the app's library gives two latency histograms. The one with a
`handler` label (`http_request_duration_seconds`) has only 3 buckets (0.1 s, 0.5 s, 1 s),
too coarse for a p95. The `highr` one has finer buckets but no `handler` label, so it
also includes the fast `/health` and `/metrics` calls. Those add 0.4 req/s per pod (one
readiness check and one scrape every 5 s), so they matter only when a pod gets under
about 8 req/s. To remove them fully, Member 1 can change one line in the app:
`Instrumentator(excluded_handlers=["/metrics", "/health"])`.

It is still an estimate: Prometheus can only interpolate inside a bucket. In a test where
the true p95 measured at the client was 15 ms, this query showed 23 ms (inside the 10–25 ms
bucket); the coarse histogram showed 95 ms. Good for watching trends on the dashboard.
**Response times in the report come from the k6 summaries**, which measure what the user
actually waits.

**3. CPU per pod, as % of its CPU request (the same measure the HPA targets)**
```promql
100 * sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="default", pod=~"web-.*", container!="", container!="POD"}[1m]))
    / sum by (pod) (kube_pod_container_resource_requests{namespace="default", pod=~"web-.*", resource="cpu"})
```
Average over all pods: wrap it in `avg( ... )`. With request 200m and limit 500m a pod
can show up to 250%. The HPA reads CPU from metrics-server, not Prometheus, so the
numbers are close but not identical.

**4. Number of pods**
```promql
kube_deployment_spec_replicas{namespace="default", deployment="web"}              # pods asked for
kube_deployment_status_replicas_available{namespace="default", deployment="web"}  # pods ready
```
Plot both. The gap between them is the scaling reaction time (one of our objectives).

## 4. Connecting from code (Member 2)

| From | Prometheus URL |
|---|---|
| Inside the cluster (a pod) | `http://monitoring-kube-prometheus-prometheus.monitoring.svc:9090` |
| On the VM | `http://localhost:9090` with the port-forward from step 2c running |

```python
import requests

PROM = "http://localhost:9090"
QUERY = 'sum(rate(http_requests_total{job="web",handler="/work"}[30s]))'

def requests_per_second():
    r = requests.get(f"{PROM}/api/v1/query", params={"query": QUERY}, timeout=5)
    r.raise_for_status()                      # error -> Prometheus down: keep current pods
    result = r.json()["data"]["result"]
    return float(result[0]["value"][1]) if result else 0.0   # empty -> no traffic
```
If the call fails, keep the current number of pods. Do not scale to 1.

## 5. What has been tested

| Item | How | Result |
|---|---|---|
| Metric names and labels | Ran `app/main.py` with Redis and read `/metrics` | `http_requests_total{handler="/work"}` and both latency histograms present as described |
| Query 1 (requests/s) | Real Prometheus scraping the app every 5 s, 10 req/s sent | 10.0 req/s; 10.4 without the `/work` filter |
| Query 1 reaction | Load jumped from 10 to 30 req/s | Half the change after 15 s, all of it after 30 s |
| Query 2 (p95) | Same run, compared with client-side p95 | 23 ms vs 15 ms true (bucket limit, see above) |
| Queries 3 and 4 | Test exporter with known CPU, requests and pod counts, including series that must be ignored | Exactly the expected values (50% and 150%; 3 asked for, 2 ready) |
| All queries | `promtool check rules` | Valid |
| Python snippet (section 4) | Run as written | Correct value; raises an error when Prometheus is down; 0.0 when there is no data |
| `servicemonitor.yaml` | Checked against the ServiceMonitor schema of chart 93.11.1 and against `k8s/web.yaml` | Valid; matches the Service (after the label) and port 8000 |

Not tested here: the install on minikube itself. The 4 checks in section 2 prove that part
on your cluster. The local test used Prometheus 2.45; the chart installs 3.16. Version 3
leaves out the one sample that sits exactly on a window's start edge, which makes no
visible difference to these queries.

## 6. Hand-off messages (copy into the group)

**To Member 2**
```
*Prometheus ready* ✅ (Member 3)
Install: `bash monitoring/install.sh` (needs Helm: `sudo snap install helm --classic`)

*Requests/s query for your autoscaler:*
`sum(rate(http_requests_total{job="web",handler="/work"}[30s]))`
• Counts /work only, app scraped every 5s, value is a 30s average
• URL in cluster: `http://monitoring-kube-prometheus-prometheus.monitoring.svc:9090`
• URL on the VM: `http://localhost:9090` after
  `kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090:9090`
• Empty result = 0 req/s. Request error = keep current pods, don't scale to 1
Python example in `monitoring/README.md` section 4.
```

**To Member 4**
```
*Grafana ready* ✅ (Member 3)
`kubectl port-forward -n monitoring svc/monitoring-grafana 3000:80` → http://localhost:3000
Login: admin / password in `monitoring/values.yaml`. Prometheus is already added as a data source.
The 4 panel queries are in `monitoring/README.md` section 3 (requests/s, p95, CPU per pod, pods).
Please save the dashboard as `monitoring/dashboard.json`.
```
