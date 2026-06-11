from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

from app.db.models import Base
from app.config import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Override the URL from app settings so we never hard-code credentials here.
config.set_main_option("sqlalchemy.url", get_settings().database_url)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # required for SQLite ALTER TABLE support
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        # Postgres has no default lock_timeout, so a DDL statement (e.g.
        # ALTER TABLE ADD COLUMN) that's blocked behind a lock held by a
        # still-running previous deploy's connection would hang forever,
        # taking the deploy (and healthcheck) down with it. Fail fast
        # instead so the deploy errors out cleanly and can be retried
        # once the old connection's transaction has released its locks.
        if connection.dialect.name == "postgresql":
            connection.exec_driver_sql("SET lock_timeout = '5s'")

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # required for SQLite ALTER TABLE support
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
