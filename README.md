# COE 452 Team 10: Kubernetes Auto-Scaling

## 1. Start the 2-node cluster (once)

    minikube start --driver=docker --nodes=2 --cpus=2 --memory=2500
    minikube addons enable metrics-server

## 2. Build and deploy the app

    docker build -t autoscale-app:v1 app/
    minikube image load autoscale-app:v1
    kubectl apply -f k8s/
    kubectl get pods -o wide

## 3. Run a load test (PATTERN = smoke, ramp, spike or periodic)

    kubectl run k6 --rm -i --restart=Never --image=grafana/k6 -- run -e PATTERN=smoke - < load/load-test.js

## 4. Capacity test on one pod (RATE = requests per second)

    kubectl scale deployment web --replicas=1
    kubectl run k6-cap --rm -i --restart=Never --image=grafana/k6 -- run -e RATE=20 - < load/capacity.js

Result: one pod handles about 20 requests/s; above that it overloads.
