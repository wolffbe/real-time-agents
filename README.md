# real-time-agents

## Setup

make start-minikube
make start-minio
make start-langfuse
make serve-langfuse

open localhost:3000, sign up, login, create org, create project, get API keys, add to .env

make start-istio

make build-agent
make build-web

make start-agent
make start-web