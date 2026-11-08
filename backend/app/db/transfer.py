"""
Copy every row of one ggNet database into another (SQLite to PostgreSQL).

    ggnet-admin copy-database sqlite:////var/lib/ggnet/ggnet.db

The target is the configured database (`[paths] database`). Both must be at
the same migration (the installer runs `alembic upgrade head` on each first)
and the target must be empty: nothing is merged or overwritten. Tables are
copied parents first in one transaction, so a failure leaves the target
empty; afterwards every table's row count is compared and PostgreSQL's id
sequences continue after the copied ids. The source is never changed.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine, func, inspect, select, text

from app.db.models import Base

CHUNK = 500


class TransferError(Exception):
    """Shown to the admin as is."""


@dataclass
class Copied:
    table: str
    rows: int


def _revision(engine: Engine) -> str | None:
    if not inspect(engine).has_table("alembic_version"):
        return None
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def copy_database(source: Engine, target: Engine) -> list[Copied]:
    src_rev, dst_rev = _revision(source), _revision(target)
    if src_rev is None or dst_rev is None:
        raise TransferError("both databases must be migrated first (alembic upgrade head)")
    if src_rev != dst_rev:
        raise TransferError(f"the databases are at different migrations ({src_rev} and {dst_rev}); "
                            "run alembic upgrade head on both")
    tables = Base.metadata.sorted_tables      # parents before the tables that point to them

    with target.connect() as conn:
        full = [t.name for t in tables if conn.execute(select(func.count()).select_from(t)).scalar()]
    if full:
        raise TransferError(f"the target database is not empty ({', '.join(full)}); nothing was copied")

    copied: list[Copied] = []
    with source.connect() as src, target.begin() as dst:
        for table in tables:
            rows = 0
            result = src.execute(select(table)).mappings()
            while chunk := result.fetchmany(CHUNK):
                dst.execute(table.insert(), [dict(r) for r in chunk])
                rows += len(chunk)
            copied.append(Copied(table.name, rows))
        if target.dialect.name == "postgresql":
            for table in tables:
                pk = list(table.primary_key.columns)
                if len(pk) == 1 and pk[0].autoincrement in (True, "auto") and pk[0].type.python_type is int:
                    col = pk[0].name
                    dst.execute(text(
                        f"SELECT setval(pg_get_serial_sequence('{table.name}', '{col}'), "
                        f"COALESCE(MAX({col}), 1), MAX({col}) IS NOT NULL) FROM {table.name}"))
        for c in copied:
            have = dst.execute(select(func.count()).select_from(Base.metadata.tables[c.table])).scalar()
            if have != c.rows:
                raise TransferError(f"{c.table}: {have} rows in the target, {c.rows} in the source")
    return copied
