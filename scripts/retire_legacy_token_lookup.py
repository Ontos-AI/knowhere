"""Inspect, explicitly retire, or restore the legacy token lookup index."""

from __future__ import annotations

import argparse
import json
import os
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL, make_url
from sqlalchemy.pool import NullPool

from shared.services.retrieval.maintenance.legacy_token_lookup import (
    LegacyTokenLookupMaintenance,
)


def main() -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    actions: argparse._MutuallyExclusiveGroup = parser.add_mutually_exclusive_group()
    actions.add_argument("--apply", action="store_true", help="Retire the legacy index")
    actions.add_argument("--restore", action="store_true", help="Rebuild the legacy index")
    parser.add_argument("--statement-timeout-seconds", type=int, default=1800)
    arguments: argparse.Namespace = parser.parse_args()
    database_url: str | None = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL must be set in the environment")
    if arguments.statement_timeout_seconds <= 0:
        parser.error("--statement-timeout-seconds must be positive")
    url: URL = make_url(database_url).set(drivername="postgresql+psycopg2")
    ssl_variables: dict[str, str] = {
        "DB_SSL_MODE": "sslmode",
        "DB_SSL_CERT": "sslcert",
        "DB_SSL_KEY": "sslkey",
        "DB_SSL_ROOT_CERT": "sslrootcert",
    }
    connection_arguments: dict[str, str] = {
        parameter: os.environ[variable]
        for variable, parameter in ssl_variables.items()
        if os.environ.get(variable)
    }
    engine: Engine = create_engine(
        url, connect_args=connection_arguments, poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
    )
    try:
        with engine.connect() as connection:
            connection.execute(text("SET lock_timeout = '5s'"))
            connection.execute(text(
                f"SET statement_timeout = '{arguments.statement_timeout_seconds}s'"
            ))
            maintenance: LegacyTokenLookupMaintenance = LegacyTokenLookupMaintenance(
                connection
            )
            if arguments.apply:
                maintenance.apply_retirement()
            elif arguments.restore:
                maintenance.restore_index()
            print(json.dumps(maintenance.inspect_indexes(), indent=2, sort_keys=True))
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        # Connection exceptions may include credentials. Report only their type.
        print(f"Index maintenance failed: {type(error).__name__}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
