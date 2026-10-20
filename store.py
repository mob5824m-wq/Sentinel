"""Generic SQLite access shared by :mod:`bot` and the feature modules.

Sentinel's per-feature modules (:mod:`applications`) keep their own tables but
must not import :mod:`bot` to reach the database helpers: ``bot.py`` imports
those modules to register their cogs, so an import back into ``bot`` would be
circular.

The helpers here are deliberately tiny and identical to the ones ``bot.py``
used before they moved: one connection per statement, opened ``with
closing(...)`` so it is always released, and rows returned as
:class:`sqlite3.Row` (``dict(row)`` when a plain mapping is wanted).

The path is read from :mod:`paths` on every call rather than captured at
import time, so a test that redirects ``SENTINEL_DATA`` before importing is
honoured, and an installed build that moved its data directory keeps working.

``bot.py`` imports these names and re-exports them, which is why the existing
``bot.db_execute`` call sites (and the dashboard, which is handed these
callables) keep working unchanged.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Optional

import paths


def db_path() -> str:
    """The SQLite file every helper here writes to."""
    return paths.DB_PATH


def db_execute(query: str, params: tuple = ()) -> None:
    """Run one statement and commit it."""
    with closing(sqlite3.connect(db_path())) as conn:
        conn.execute(query, params)
        conn.commit()


def db_fetchall(query: str, params: tuple = ()) -> list[sqlite3.Row]:
    """Run one query and return every row."""
    with closing(sqlite3.connect(db_path())) as conn:
        conn.row_factory = sqlite3.Row
        return list(conn.execute(query, params).fetchall())


def db_fetchone(query: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    """Run one query and return the first row (or ``None``)."""
    with closing(sqlite3.connect(db_path())) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(query, params).fetchone()


def db_insert(query: str, params: tuple = ()) -> int:
    """Run one INSERT and return the new row id (``lastrowid``)."""
    with closing(sqlite3.connect(db_path())) as conn:
        cursor = conn.execute(query, params)
        conn.commit()
        return int(cursor.lastrowid or 0)
