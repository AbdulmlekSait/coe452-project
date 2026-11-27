# Autoscaler (Member 2): custom autoscaler and HPA YAML files

| File | What it is |
|---|---|
| `hpa-cpu30.yaml`, `hpa-cpu50.yaml`, `hpa-cpu70.yaml` | Built-in HPA (`autoscaling/v2`) for `web`: 1 to 10 pods, target average CPU 30 / 50 / 70% of the 200m request. Other targets: the runner makes them from `hpa-cpu50.yaml` (e.g. `--settings hpa-100`) |
| `custom_autoscaler.py` | Our autoscaler: scales `web` on requests per second read from Prometheus |
| `pod_logger.py` | Writes the number of pods (and HPA CPU %, if an HPA exists) to CSV every 5 s |
| `requirements.txt` | Python packages for both scripts |

## Setup (once, on the VM)

    sudo apt install -y python3-venv
    python3 -m venv ~/venv
    source ~/venv/bin/activate          # run this in every new terminal
    pip install -r autoscaler/requirements.txt

The scripts use your normal kubeconfig, so they work wherever `kubectl` works.

## Built-in HPA

    kubectl apply -f autoscaler/hpa-cpu50.yaml
    kubectl get hpa web --watch           # TARGETS shows current/target CPU
    kubectl delete hpa web                # always before testing another setting

The HPA needs `minikube addons enable metrics-server`. By default it waits 5 minutes
of low load before removing pods (scale-down stabilization).

## Custom autoscaler

Every 10 s it reads total requests/s from Prometheus and sets

    pods = ceil(requests per second / per-pod target - 0.1), kept between 1 and 10

The 0.1 tolerance (like the HPA's default 10%) stops a reading just above a multiple of
the target from adding a pod: 40.04 req/s with target 20 gives 2 pods, not 3; 43 req/s gives 3.

- **Scale up** at once, at most once every 20 s (`--up-cooldown`), so new pods have time to become ready.
- **Scale down** only when the desired value stayed lower for 60 s (`--down-window`); it then
  uses the highest value seen in those 60 s, so a short dip does not remove pods.
- Every decision is written to a CSV file: time, requests/s, current pods, desired pods, action, reason.
- If Prometheus or the API is briefly unreachable, it logs an `error` row and keeps going.

Prometheus must be reachable from the VM. In a separate terminal:

    kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090:9090

Then (with the HPA deleted):

    kubectl delete hpa web --ignore-not-found
    python3 autoscaler/custom_autoscaler.py --target 20 --csv decisions.csv
    python3 autoscaler/custom_autoscaler.py --target 15 --csv decisions.csv

Default query (Member 3's, `/work` only, 30 s average; change with `--query`):

    sum(rate(http_requests_total{job="web",handler="/work"}[30s]))

If the query returns nothing, it checks `up{job="web"}`: no target means Prometheus is not
scraping the app (e.g. the `web` Service lost its `app=web` label), so it logs an error and
keeps the current pods instead of treating it as 0 req/s.

Test without changing anything: add `--dry-run`. Stop with Ctrl+C.

Targets: Member 1's capacity test found 1 pod handles about 20 req/s, so
`--target 20` runs pods at their limit and `--target 15` keeps about 25% headroom.
