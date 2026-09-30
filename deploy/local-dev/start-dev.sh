#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.dev.yml"

log_step() {
    printf '%s\n' "$1"
}

print_usage() {
    cat <<EOF
Usage: ./start-dev.sh

Options:
  -h, --help   Show this help message.
EOF
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -h|--help)
                print_usage
                exit 0
                ;;
            *)
                printf 'Unknown option: %s\n' "$1" >&2
                print_usage >&2
                exit 1
                ;;
        esac
        shift
    done
}

run_compose() {
    if command -v docker-compose >/dev/null 2>&1; then
        docker-compose "$@"
        return 0
    fi

    docker compose "$@"
}

require_docker() {
    if ! docker info >/dev/null 2>&1; then
        printf 'Docker is not running. Start Docker first.\n' >&2
        exit 1
    fi
}

wait_for_postgres() {
    log_step "Waiting for PostgreSQL..."

    for _attempt in $(seq 1 30); do
        if docker exec knowhere_postgres pg_isready -U root -d Knowhere >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done

    printf 'PostgreSQL did not become ready in time.\n' >&2
    exit 1
}

wait_for_redis() {
    log_step "Waiting for Redis..."

    for _attempt in $(seq 1 30); do
        if docker exec knowhere_redis redis-cli ping >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done

    printf 'Redis did not become ready in time.\n' >&2
    exit 1
}

wait_for_localstack() {
    log_step "Waiting for LocalStack..."

    for _attempt in $(seq 1 30); do
        if curl -fsS http://localhost:4566/_localstack/health >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done

    printf 'LocalStack did not become ready in time.\n' >&2
    exit 1
}

print_summary() {
    cat <<EOF

Local development services are ready.

Service endpoints:
  - LocalStack: http://localhost:4566
  - PostgreSQL: localhost:5432 (root/root123)
  - Redis: localhost:6379

Next steps:
  1. Start the API: cd apps/api && uv run --env-file .env uvicorn main:app --host 0.0.0.0 --port 5005 --reload
  2. Start the worker: cd apps/worker && uv run --env-file .env python worker.py

Stop services:
  ${SCRIPT_DIR}/stop-dev.sh
EOF
}

main() {
    parse_args "$@"
    log_step "Starting Knowhere local development services..."
    require_docker

    run_compose -f "${COMPOSE_FILE}" up -d

    wait_for_postgres
    wait_for_redis
    wait_for_localstack
    print_summary
}

main "$@"
