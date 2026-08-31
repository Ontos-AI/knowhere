from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection, Engine

from alembic import context

# Import the shared database configuration and metadata.
from shared.core.config import settings
from shared.core.database import Base
from shared.models import database as shared_database_models  # noqa: F401
from shared.services.auth.user_table_bootstrap import ensure_better_auth_user_table

# Build a synchronous database URL by replacing asyncpg with psycopg2.
sync_database_url = settings.DATABASE_URL.replace("asyncpg", "psycopg2")

# Read SSL connect args from shared settings.
ssl_connect_args = settings.get_ssl_connect_args()

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
target_metadata = Base.metadata

_EXTERNALLY_MANAGED_TABLES: frozenset[str] = frozenset(
    {
        "user",
        "verification",
        "jwks",
        "account",
        "emailVerificationToken",
        "session",
    }
)
_AUTOGENERATE_IGNORED_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {
        ("document_chunks", "content_search_tsv"),
        ("document_chunks", "path_search_tsv"),
    }
)


def _resolve_table_name(object_: object, compare_to: object | None) -> str | None:
    for candidate in (object_, compare_to):
        table = getattr(candidate, "table", None)
        table_name = getattr(table, "name", None)
        if isinstance(table_name, str):
            return table_name
    return None


def include_object(
    object_: object,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: object | None,
) -> bool:
    """Exclude externally managed auth tables and generated TSV columns."""
    del reflected
    if type_ == "table" and isinstance(name, str) and name in _EXTERNALLY_MANAGED_TABLES:
        return False
    if type_ == "column" and isinstance(name, str):
        table_name = _resolve_table_name(object_, compare_to)
        if table_name is not None and (table_name, name) in _AUTOGENERATE_IGNORED_COLUMNS:
            return False
    return True


# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    # Use the shared synchronous database configuration.
    url = sync_database_url
    context.configure(
        url=url,
        target_metadata=target_metadata,
        include_object=include_object,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Pass through configured SSL connect args.
        connect_args=ssl_connect_args,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    configured_connection = config.attributes.get("connection")

    def run_with_connection(connection: Connection) -> None:
        if settings.API_STANDALONE_MODE_ENABLED:
            ensure_better_auth_user_table(connection)

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()

    if isinstance(configured_connection, Connection):
        run_with_connection(configured_connection)
        return

    if isinstance(configured_connection, Engine):
        with configured_connection.begin() as connection:
            run_with_connection(connection)
        return

    # Use create_engine directly so SSL connect args are applied explicitly.
    from sqlalchemy import create_engine

    connectable = create_engine(
        sync_database_url,
        poolclass=pool.NullPool,
        connect_args=ssl_connect_args,
    )

    with connectable.begin() as connection:
        run_with_connection(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
