#!/usr/bin/env python3
"""Load local .env files into the process, then run the live API + worker.

Fine title detection reads IMAGE_MODEL from the process environment, not
from pydantic settings. Starting `uv run worker.py` with a partial export
skips VLM and leaves fat leaves unsplit. This script is the local entry
that must be used instead of hand-started processes.

Usage (from repo root):
  ./deploy/local-dev/debug-live.sh check
  ./deploy/local-dev/debug-live.sh start
  ./deploy/local-dev/debug-live.sh stop
  ./deploy/local-dev/debug-live.sh ensure-credits
  ./deploy/local-dev/debug-live.sh requeue-scope --document-id doc_xxx --scope-id p481-634
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
API_ROOT = ROOT / "apps" / "api"
WORKER_ROOT = ROOT / "apps" / "worker"
PID_PATH = Path("/tmp/knowhere-debug-live.json")
REQUIRED_ENV = ("IMAGE_MODEL", "DATABASE_URL")
CREDIT_TOP_UP = 50_000_000


def _load_env_file(path: Path, env: dict[str, str]) -> None:
    if not path.is_file():
        raise SystemExit(f"missing env file: {path}")
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key.startswith("#"):
            continue
        env[key] = value.strip().strip("'").strip('"')


def load_debug_env(*, database_url: str | None = None) -> dict[str, str]:
    env = dict(os.environ)
    _load_env_file(API_ROOT / ".env", env)
    _load_env_file(WORKER_ROOT / ".env", env)
    if database_url:
        env["DATABASE_URL"] = database_url
    env.setdefault("LOCAL_DEBUG", "1")
    return env


def _redact_db(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or "?"
    port = parsed.port or "?"
    db = (parsed.path or "/").lstrip("/") or "?"
    return f"{host}:{port}/{db}"


def _present(env: Mapping[str, str], key: str) -> str:
    value = str(env.get(key) or "").strip()
    return f"set({len(value)})" if value else "MISSING"


def check_env(env: Mapping[str, str]) -> None:
    missing = [key for key in REQUIRED_ENV if not str(env.get(key) or "").strip()]
    print(f"IMAGE_MODEL={_present(env, 'IMAGE_MODEL')}")
    print(f"CURSOR_API_KEY={_present(env, 'CURSOR_API_KEY')}")
    print(f"ALI_API_KEYS={_present(env, 'ALI_API_KEYS')}")
    print(f"DATABASE_URL={_redact_db(str(env.get('DATABASE_URL') or ''))}")
    if missing:
        raise SystemExit("refusing to start: " + ", ".join(missing) + " empty after loading .env")


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _read_pids() -> dict[str, int]:
    if not PID_PATH.is_file():
        return {}
    try:
        raw = json.loads(PID_PATH.read_text())
    except json.JSONDecodeError:
        return {}
    return {str(name): int(pid) for name, pid in raw.items()}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _write_pids(pids: dict[str, int]) -> None:
    PID_PATH.write_text(json.dumps(pids, indent=2))


def _spawn(name: str, cwd: Path, args: list[str], env: dict[str, str]) -> int:
    log_path = Path(f"/tmp/knowhere-debug-live-{name}.log")
    log_file = log_path.open("ab")
    proc = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    print(f"{name} pid={proc.pid} log={log_path}")
    return proc.pid


def cmd_start(args: argparse.Namespace) -> None:
    env = load_debug_env(database_url=args.database_url)
    check_env(env)
    existing = _read_pids()
    live = {name: pid for name, pid in existing.items() if _pid_alive(pid)}
    if live:
        raise SystemExit(f"already running: {live}. stop first.")
    api_pid = _spawn("api", API_ROOT, ["uv", "run", "main.py"], env)
    worker_pid = _spawn("worker", WORKER_ROOT, ["uv", "run", "worker.py"], env)
    _write_pids({"api": api_pid, "worker": worker_pid})
    deadline = time.time() + 45
    while time.time() < deadline:
        if _port_open(5005):
            print("api listening on :5005")
            return
        if not _pid_alive(api_pid):
            raise SystemExit("api exited during startup; see /tmp/knowhere-debug-live-api.log")
        time.sleep(0.5)
    raise SystemExit("api did not bind :5005 in time")


def cmd_stop(_args: argparse.Namespace) -> None:
    pids = _read_pids()
    for name, pid in pids.items():
        if not _pid_alive(pid):
            print(f"{name} already stopped")
            continue
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            print(f"{name} already stopped")
            continue
        print(f"stopped {name} pid={pid}")
    if PID_PATH.exists():
        PID_PATH.unlink()


def cmd_check(args: argparse.Namespace) -> None:
    env = load_debug_env(database_url=args.database_url)
    check_env(env)
    print(f"api_port_5005={'up' if _port_open(5005) else 'down'}")
    pids = _read_pids()
    for name, pid in pids.items():
        print(f"{name} pid={pid} {'alive' if _pid_alive(pid) else 'dead'}")


def _psql(env: Mapping[str, str], sql: str) -> str:
    parsed = urlparse(str(env["DATABASE_URL"]))
    proc = subprocess.run(
        [
            "psql",
            "-h",
            parsed.hostname or "127.0.0.1",
            "-p",
            str(parsed.port or 5432),
            "-U",
            parsed.username or "knowhere_runtime",
            "-d",
            (parsed.path or "/Knowhere").lstrip("/"),
            "-t",
            "-A",
            "-c",
            sql,
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PGPASSWORD": parsed.password or ""},
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(proc.stderr.strip() or "psql failed")
    return proc.stdout.strip()


def cmd_ensure_credits(args: argparse.Namespace) -> None:
    env = load_debug_env(database_url=args.database_url)
    check_env(env)
    user_id = args.user_id
    _psql(
        env,
        f"""
        UPDATE user_balances
        SET credits_balance = credits_balance + {CREDIT_TOP_UP},
            updated_at = NOW()
        WHERE user_id = '{user_id}';
        INSERT INTO credits_transactions (
          id, user_id, credits_amount, transaction_type, description, created_at
        ) VALUES (
          'topup_' || substr(md5(random()::text), 1, 24),
          '{user_id}',
          {CREDIT_TOP_UP},
          'purchase',
          'Local debug-live credit top-up',
          NOW()
        );
        """,
    )
    balance = _psql(
        env,
        f"SELECT credits_balance FROM user_balances WHERE user_id='{user_id}';",
    )
    print(f"credits_balance={balance}")


def cmd_requeue_scope(args: argparse.Namespace) -> None:
    env = load_debug_env(database_url=args.database_url)
    check_env(env)
    document_id = args.document_id
    scope_id = args.scope_id
    row = _psql(
        env,
        f"""
        UPDATE document_scope_enrichments
        SET state='queued', priority='high', refined_at=NULL, updated_at=NOW()
        WHERE document_id='{document_id}' AND scope_id='{scope_id}'
        RETURNING job_result_id;
        """,
    )
    if not row:
        raise SystemExit(f"no scope {scope_id} for {document_id}")
    job_result_id = row.splitlines()[0]
    send = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "-c",
            (
                "from shared.core.celery_app import get_celery_app;"
                "r=get_celery_app().send_task("
                "'app.core.tasks.enrichment_tasks.enrich_page_memory_scope',"
                f"args=['{job_result_id}','{scope_id}'],"
                "queue='document_ingestion_high',priority=9);"
                "print(r.id)"
            ),
        ],
        cwd=WORKER_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if send.returncode != 0:
        raise SystemExit(send.stderr.strip() or send.stdout.strip() or "enqueue failed")
    print(f"requeued scope={scope_id} job_result_id={job_result_id} task={send.stdout.strip()}")


def cmd_health(_args: argparse.Namespace) -> None:
    try:
        with urllib.request.urlopen("http://127.0.0.1:5005/", timeout=3) as resp:
            print(f"api http={resp.status}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"api down: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the local live debug stack with full .env")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="Override DATABASE_URL after loading .env files",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="Print whether required env is present; do not start")
    sub.add_parser("start", help="Start API + worker with loaded .env")
    sub.add_parser("stop", help="Stop processes started by this script")
    sub.add_parser("health", help="Hit local API :5005")
    credits = sub.add_parser("ensure-credits", help="Add debug credits for the local user")
    credits.add_argument("--user-id", default="debug_local_user")
    requeue = sub.add_parser("requeue-scope", help="Reset one scope to queued and enqueue Fine")
    requeue.add_argument("--document-id", required=True)
    requeue.add_argument("--scope-id", required=True)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    commands = {
        "check": cmd_check,
        "start": cmd_start,
        "stop": cmd_stop,
        "health": cmd_health,
        "ensure-credits": cmd_ensure_credits,
        "requeue-scope": cmd_requeue_scope,
    }
    commands[args.command](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
