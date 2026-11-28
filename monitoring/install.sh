#!/usr/bin/env bash
# Installs Prometheus + Grafana (kube-prometheus-stack) into the "monitoring" namespace,
# then tells Prometheus to scrape the web app.
# Run from the repo root, after README steps 1-2 (cluster + app running):
#   bash monitoring/install.sh
set -euo pipefail

RELEASE=monitoring
NAMESPACE=monitoring
CHART_VERSION=93.11.1   # pinned so all 4 members get the same setup.
                        # If Helm says this version is not found, run:
                        #   helm search repo prometheus-community/kube-prometheus-stack
                        # and put the version it shows here.

if ! command -v helm >/dev/null 2>&1; then
  echo "Helm is not installed. Install it with:  sudo snap install helm --classic"
  exit 1
fi

if ! kubectl get svc web >/dev/null 2>&1; then
  echo "The web Service is not running. Do README steps 1-2 first."
  exit 1
fi

# The ServiceMonitor finds the Service by the label app=web, which k8s/web.yaml does
# not set yet. Add it here so monitoring works now; it survives "kubectl apply -f k8s/".
# If the Service is deleted and created again, run this script again.
kubectl label svc web app=web --overwrite

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update
helm repo update

helm upgrade --install "$RELEASE" prometheus-community/kube-prometheus-stack \
  --namespace "$NAMESPACE" --create-namespace \
  --version "$CHART_VERSION" \
  -f monitoring/values.yaml \
  --wait --timeout 10m

# The ServiceMonitor type only exists after the chart is installed, so apply it last.
kubectl apply -f monitoring/servicemonitor.yaml

kubectl get pods -n "$NAMESPACE"

cat <<EOF

Done. To open the web pages, run each in its own terminal:
  Prometheus: kubectl port-forward -n $NAMESPACE svc/$RELEASE-kube-prometheus-prometheus 9090:9090
              then http://localhost:9090/targets  (serviceMonitor/monitoring/web/0 must be UP)
  Grafana:    kubectl port-forward -n $NAMESPACE svc/$RELEASE-grafana 3000:80
              then http://localhost:3000  (admin / password in monitoring/values.yaml)

To remove everything:  helm uninstall $RELEASE -n $NAMESPACE && kubectl delete namespace $NAMESPACE
EOF
