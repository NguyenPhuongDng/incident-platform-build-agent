"""SQLite engine + session helpers."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from backend.app.config import settings
from backend.app import models  # noqa: F401  (registers tables)

logger = logging.getLogger("db")

engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False},
    echo=False,
)


def _patch_missing_columns() -> None:
    """SQLite's `create_all()` never alters an existing table, so a field added to
    a model after the table was first created (e.g. `Ticket.is_eval`) would be
    silently missing on any pre-existing demo.db and crash the first query that
    touches it. This adds those columns in place, best-effort, so old databases
    (including a live one from before this code shipped) keep working."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # brand-new table: create_all() below makes it with every column
            existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing_cols:
                    continue
                default_sql = ""
                arg = getattr(col.default, "arg", None)
                if isinstance(arg, bool):
                    default_sql = f" DEFAULT {1 if arg else 0}"
                elif isinstance(arg, (int, float)):
                    default_sql = f" DEFAULT {arg}"
                elif isinstance(arg, str):
                    default_sql = f" DEFAULT '{arg}'"
                col_type = col.type.compile(engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col_type}{default_sql}'))
                logger.info("db_migrate: thêm cột %s.%s", table.name, col.name)


def init_db() -> None:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    _patch_missing_columns()
    SQLModel.metadata.create_all(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with Session(engine) as session:
        yield session
