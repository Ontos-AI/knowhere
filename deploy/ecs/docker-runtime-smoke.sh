#!/usr/bin/env bash

set -euo pipefail

NETWORK_NAME="knowhere-runtime-smoke"
POSTGRES_CONTAINER="knowhere-runtime-smoke-postgres"
REDIS_CONTAINER="knowhere-runtime-smoke-redis"
API_CONTAINER="knowhere-runtime-smoke-api"
WORKER_CONTAINER="knowhere-runtime-smoke-worker"
API_IMAGE="${API_IMAGE:-knowhere-runtime-smoke-api:local}"
WORKER_IMAGE="${WORKER_IMAGE:-knowhere-runtime-smoke-worker:local}"
POSTGRES_IMAGE="${POSTGRES_IMAGE:-postgres:15-alpine}"
REDIS_IMAGE="${REDIS_IMAGE:-redis:7-alpine}"

cleanup() {
    docker rm --force \
        "$API_CONTAINER" \
        "$WORKER_CONTAINER" \
        "$POSTGRES_CONTAINER" \
        "$REDIS_CONTAINER" >/dev/null 2>&1 || true
    docker network rm "$NETWORK_NAME" >/dev/null 2>&1 || true
}

trap cleanup EXIT

wait_for_postgres() {
    for _attempt in $(seq 1 60); do
        if docker exec "$POSTGRES_CONTAINER" pg_isready -U root -d Knowhere >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    docker logs "$POSTGRES_CONTAINER" || true
    return 1
}

wait_for_redis() {
    for _attempt in $(seq 1 60); do
        if docker exec "$REDIS_CONTAINER" redis-cli ping >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    docker logs "$REDIS_CONTAINER" || true
    return 1
}

wait_for_api() {
    for _attempt in $(seq 1 90); do
        if docker exec "$API_CONTAINER" curl --fail --silent http://localhost:5005/health >/dev/null 2>&1; then
            return 0
        fi
        if ! docker inspect --format '{{.State.Running}}' "$API_CONTAINER" 2>/dev/null | grep --quiet true; then
            docker logs "$API_CONTAINER" || true
            return 1
        fi
        sleep 2
    done
    docker logs "$API_CONTAINER" || true
    return 1
}

wait_for_worker() {
    for _attempt in $(seq 1 90); do
        if docker exec "$WORKER_CONTAINER" \
            python -c 'from shared.services.worker_health import assert_worker_healthy; assert_worker_healthy()' >/dev/null 2>&1; then
            return 0
        fi
        if ! docker inspect --format '{{.State.Running}}' "$WORKER_CONTAINER" 2>/dev/null | grep --quiet true; then
            docker logs "$WORKER_CONTAINER" || true
            return 1
        fi
        sleep 2
    done
    docker logs "$WORKER_CONTAINER" || true
    return 1
}

cleanup
docker network create "$NETWORK_NAME" >/dev/null

docker run --detach --name "$POSTGRES_CONTAINER" --network "$NETWORK_NAME" \
    --env POSTGRES_DB=Knowhere \
    --env POSTGRES_USER=root \
    --env POSTGRES_PASSWORD=root123 \
    "$POSTGRES_IMAGE" >/dev/null
docker run --detach --name "$REDIS_CONTAINER" --network "$NETWORK_NAME" \
    "$REDIS_IMAGE" >/dev/null

wait_for_postgres
wait_for_redis

build_arguments=()
if [[ -n "${http_proxy:-}" ]]; then
    build_arguments+=(--build-arg "http_proxy=${http_proxy}")
fi
if [[ -n "${https_proxy:-}" ]]; then
    build_arguments+=(--build-arg "https_proxy=${https_proxy}")
fi

docker build "${build_arguments[@]}" --file deploy/docker/Dockerfile.api --tag "$API_IMAGE" .
docker build "${build_arguments[@]}" --file deploy/docker/Dockerfile.worker --tag "$WORKER_IMAGE" .

common_environment=(
    --env ENVIRONMENT=development
    --env APP_ENV=development
    --env TMP_PATH=/tmp/knowhere
    --env DATABASE_URL=postgresql+asyncpg://root:root123@${POSTGRES_CONTAINER}:5432/Knowhere
    --env DB_SSL_MODE=disable
    --env DB_POOL_SIZE=2
    --env DB_MAX_OVERFLOW=2
    --env DB_SYNC_POOL_SIZE=2
    --env DB_SYNC_MAX_OVERFLOW=2
    --env REDIS_HOST="$REDIS_CONTAINER"
    --env REDIS_PORT=6379
    --env REDIS_DATABASE=0
    --env REDIS_SSL=false
    --env CELERY_REDIS_URL=redis://${REDIS_CONTAINER}:6379/0
    --env S3_TYPE=filesystem
    --env S3_BUCKET_NAME=knowhere-runtime-smoke
    --env S3_RESULTS_BUCKET=knowhere-runtime-smoke
    --env S3_TEMP_PATH=/tmp
    --env OBJECT_STORAGE_LOCAL_ROOT=/tmp/object-storage
    --env TELEMETRY_ENABLED=false
    --env LOGFIRE_TOKEN=
    --env LLM_MOCK_ENABLED=true
    --env BILLING_ENABLED=false
    --env API_STANDALONE_MODE_ENABLED=true
    --env RATE_LIMIT_ENABLED=false
)

docker run --rm --network "$NETWORK_NAME" \
    "${common_environment[@]}" \
    --entrypoint python "$API_IMAGE" -m alembic upgrade heads

docker run --detach --name "$API_CONTAINER" --network "$NETWORK_NAME" \
    "${common_environment[@]}" \
    --env INTERNAL_DASHBOARD_ENDPOINT=http://localhost:3000 \
    "$API_IMAGE" >/dev/null

docker run --detach --name "$WORKER_CONTAINER" --network "$NETWORK_NAME" \
    "${common_environment[@]}" \
    --env WORKER_CONCURRENCY=1 \
    "$WORKER_IMAGE" >/dev/null

wait_for_api
wait_for_worker

printf 'Docker runtime smoke test passed.\n'
printf '  API image: %s\n' "$API_IMAGE"
printf '  Worker image: %s\n' "$WORKER_IMAGE"
