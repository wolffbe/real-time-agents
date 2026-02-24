.PHONY: start-minikube stop-minikube start-minio stop-minio start-langfuse stop-langfuse serve-langfuse unserve-langfuse build-agent build-web start-istio stop-istio start-agent stop-agent start-web stop-web serve-web unserve-web create-hopsworks-secret

# ====== Minikube ======
start-minikube:
	@echo "Starting Minikube..."
	@minikube status > /dev/null 2>&1 || minikube start --memory=24576 --cpus=8
	@eval $$(minikube docker-env) && echo "Docker environment configured"
	@echo "✓ Minikube started"

stop-minikube:
	@echo "Stopping Minikube..."
	@minikube stop || true
	@echo "✓ Minikube stopped"

# ====== Minio ======
start-minio:
	@echo "Deploying Minio..."
	@helm repo add minio https://charts.min.io/ || true
	@helm repo update minio
	@helm install minio minio/minio -n minio --create-namespace \
		--set resources.requests.memory=512Mi \
		--set replicas=1 \
		--set persistence.enabled=false \
		--set mode=standalone \
		--set rootUser=minio,rootPassword=miniosecret || true
	@echo "Waiting for Minio pod to be ready..."
	@kubectl wait --for=condition=ready pod -l app=minio -n minio --timeout=120s || true
	@kubectl get pods -n minio
	@echo "✓ Minio deployed"

stop-minio:
	@echo "Uninstalling Minio..."
	@helm uninstall minio -n minio || true
	@kubectl delete namespace minio --ignore-not-found || true
	@echo "✓ Minio removed"

# ====== Langfuse ======
start-langfuse:
	@echo "Ensuring Minio bucket 'langfuse' exists..."
	@MINIO_POD=$$(kubectl get pods -n minio -l release=minio -o jsonpath="{.items[0].metadata.name}") && \
		kubectl exec -n minio $$MINIO_POD -- mc alias set myminio http://localhost:9000 minio miniosecret --api S3v4 && \
		kubectl exec -n minio $$MINIO_POD -- mc mb myminio/langfuse --ignore-existing
	@echo "✓ Minio bucket ready"
	@echo "Deploying Langfuse..."
	@helm repo add langfuse https://langfuse.github.io/langfuse-k8s || true
	@helm repo update
	@helm install langfuse langfuse/langfuse -n langfuse -f ./helm/langfuse/values.yaml --create-namespace || true
	@echo "Waiting for pods to be ready..."
	@kubectl wait --for=condition=ready pod -l app.kubernetes.io/instance=langfuse -n langfuse --timeout=300s || true
	@kubectl get pods -n langfuse
	@echo "✓ Langfuse deployed"

stop-langfuse:
	@echo "Uninstalling Langfuse..."
	@helm uninstall langfuse -n langfuse || true
	@kubectl delete namespace langfuse --ignore-not-found || true
	@echo "✓ Langfuse removed"

serve-langfuse:
	@echo "Serving Langfuse..."
	@nohup kubectl port-forward svc/langfuse-web 3000:3000 -n langfuse > /dev/null 2>&1 &
	@echo "Langfuse available at http://localhost:3000"

unserve-langfuse:
	@echo "Stopping Langfuse port-forward..."
	@pkill -f "kubectl port-forward.*langfuse" || true
	@echo "Langfuse port-forward stopped."

# ====== Istio ======
start-istio:
	@echo "Installing Istio..."
	@which istioctl > /dev/null || (echo "Installing istioctl..." && brew install istioctl)
	@istioctl install --set profile=demo -y
	@echo "Waiting for Istio pods..."
	@kubectl wait --for=condition=ready pod -l app=istiod -n istio-system --timeout=120s || true
	@kubectl get pods -n istio-system
	@echo "✓ Istio installed"

stop-istio:
	@echo "Uninstalling Istio..."
	@istioctl uninstall --purge -y || true
	@kubectl delete namespace istio-system --ignore-not-found || true
	@echo "✓ Istio removed"

# ====== Build ======
build-agent:
	@echo "Building agent image..."
	@eval $$(minikube docker-env) && docker build -t real-time-agents/agent:latest ./agent
	@echo "✓ Agent image built"

build-web:
	@echo "Building web image..."
	@eval $$(minikube docker-env) && docker build -t real-time-agents/web:latest ./web
	@echo "✓ Web image built"

# ====== Hopsworks ======
create-hopsworks-secret:
	@echo "Creating Hopsworks secret..."
	@kubectl create secret generic hopsworks-secrets \
		--from-literal=HOPSWORKS_API_KEY=$$HOPSWORKS_API_KEY \
		--from-literal=HOPSWORKS_PROJECT=$$HOPSWORKS_PROJECT \
		--from-literal=HOPSWORKS_HOST=$$HOPSWORKS_HOST \
		-n real-time-agents \
		--dry-run=client -o yaml | kubectl apply -f -
	@echo "✓ Hopsworks secret created"

# ====== Agent ======
start-agent:
	@echo "Deploying agent..."
	@kubectl create namespace real-time-agents --dry-run=client -o yaml | kubectl apply -f -
	@kubectl label namespace real-time-agents istio-injection=enabled --overwrite || true
	@kubectl create secret generic agent-secrets --from-env-file=.env -n real-time-agents --dry-run=client -o yaml | kubectl apply -f -
	@kubectl apply -f ./k8s/agent.yaml
	@echo "Waiting for agent pod..."
	@kubectl wait --for=condition=ready pod -l app=agent -n real-time-agents --timeout=120s || true
	@kubectl get pods -n real-time-agents -l app=agent
	@echo "✓ Agent deployed"

stop-agent:
	@echo "Removing agent..."
	@kubectl delete -f ./k8s/agent.yaml --ignore-not-found || true
	@kubectl delete secret agent-secrets -n real-time-agents --ignore-not-found || true
	@echo "✓ Agent removed"

# ====== App (Web) ======
start-web:
	@echo "Deploying web app..."
	@kubectl create namespace real-time-agents --dry-run=client -o yaml | kubectl apply -f -
	@kubectl label namespace real-time-agents istio-injection=enabled --overwrite || true
	@$(MAKE) create-hopsworks-secret
	@kubectl apply -f ./k8s/web.yaml
	@echo "Waiting for web pod..."
	@kubectl wait --for=condition=ready pod -l app=web -n real-time-agents --timeout=120s || true
	@kubectl get pods -n real-time-agents -l app=web
	@kubectl apply -f ./k8s/istio.yaml
	@echo "✓ Web app deployed with Istio"
	@echo ""
	@echo "To access: kubectl port-forward svc/istio-ingressgateway 8080:80 -n istio-system"

stop-web:
	@echo "Removing web app and Istio resources..."
	@kubectl delete -f ./k8s/istio.yaml --ignore-not-found || true
	@kubectl delete -f ./k8s/web.yaml --ignore-not-found || true
	@echo "✓ Web app removed"

serve-web:
	@echo "Serving web app via Istio gateway..."
	@nohup kubectl port-forward svc/istio-ingressgateway 8080:80 -n istio-system > /dev/null 2>&1 &
	@echo "Web app available at http://localhost:8080"

unserve-web:
	@echo "Stopping web port-forward..."
	@pkill -f "kubectl port-forward.*istio-ingressgateway" || true
	@echo "Web port-forward stopped."
