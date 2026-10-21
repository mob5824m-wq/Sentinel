"""Guard: file-backed SQLite connections must be closed, not garbage collected.

Why this exists (see ``docs/CI_FIXES.md``): ``with sqlite3.connect(...) as conn``
commits on success but does **not** close the connection — the connection's
context manager is a transaction, not a resource manager. Code that relies on
the connection being collected instead is fine on Linux and macOS, where an
open file can still be unlinked, and fails on Windows with:

    PermissionError: [WinError 32] The process cannot access the file because
    it is being used by another process: '...\\sentinel.db'

That is exactly how both Windows CI legs went red while the other four stayed
green: the temporary directory holding a sentinel.db could not be removed
because a test had left a handle on it. The fix is ``contextlib.closing(...)``,
the convention ``bot.py``, ``store.py`` and ``legacy_cleanup.py`` already use.

A Windows-only failure is expensive to discover — it needs a Windows run to
show up — so this module checks the property directly and on every platform:
the database tests in ``tests/test_legacy_cleanup.py`` are run again with every
``sqlite3.connect()`` traced, and each connection made to a real *file* has to
be closed by the time its test finishes.

Why only file-backed connections: an in-memory database (``":memory:"``) holds
no file, so nothing can block an unlink and there is no Windows-only failure to
catch. Those are skipped rather than enforced, which keeps the check aimed at
the failure it exists to prevent instead of at style.

Why the trace holds a reference to each connection: that is what makes a leak
visible. If nothing referenced the connection, the reference count could drop
to zero and CPython would close it for us — at a moment the test cannot
predict, which is precisely what made the original bug intermittent-looking.
Holding the reference asks the strict question: was it closed *explicitly*?

Run with either:
    python -m pytest tests/test_sqlite_handles.py
    python tests/test_sqlite_handles.py
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TESTS_DIR = Path(__file__).resolve().parent

#: The module whose tests touch a database file. It is imported by path, under
#: a private name, so this check cannot collide with pytest's own module name.
GUARDED_MODULE = "test_legacy_cleanup"
GUARDED_MODULE_ALIAS = "_legacy_cleanup_under_handle_check"


def _load_guarded_module() -> object:
    """Import a test module from this directory without pytest's help."""
    if GUARDED_MODULE_ALIAS in sys.modules:
        return sys.modules[GUARDED_MODULE_ALIAS]
    spec = importlib.util.spec_from_file_location(
        GUARDED_MODULE_ALIAS, TESTS_DIR / f"{GUARDED_MODULE}.py"
    )
    if spec is None or spec.loader is None:  # pragma: no cover - packaging error
        raise AssertionError(f"could not load tests/{GUARDED_MODULE}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[GUARDED_MODULE_ALIAS] = module
    spec.loader.exec_module(module)
    return module


def _database_of(args: tuple, kwargs: dict) -> object:
    """The ``database`` argument handed to ``sqlite3.connect``."""
    if "database" in kwargs:
        return kwargs["database"]
    return args[0] if args else ":memory:"


def _is_in_memory(database: object) -> bool:
    """Whether a connect target keeps its data in memory rather than in a file."""
    if database is None:
        return True        # sqlite3 turns this into an in-memory database
    if isinstance(database, (bytes, bytearray)):
        return False
    text = str(database)
    if text in ("", ":memory:"):
        return True
    # URI filenames: "file::memory:?cache=shared" and friends.
    return text.startswith("file::memory:") or "mode=memory" in text


def _is_open(connection: sqlite3.Connection) -> bool:
    """Whether the connection still holds the database open.

    sqlite3 does not expose a ``closed`` flag, so this asks the database: a
    closed connection refuses to run a statement. ``SELECT 1`` on an open one
    touches no user table and changes nothing.
    """
    try:
        connection.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return False
    except sqlite3.Error:  # pragma: no cover - not expected for SELECT 1
        return True
    return True


class ClosedDatabaseHandlesTests(unittest.TestCase):
    def test_the_cleanup_tests_close_every_file_backed_connection(self) -> None:
        module = _load_guarded_module()
        opened: list[tuple[str, sqlite3.Connection]] = []
        real_connect = sqlite3.connect

        def tracing_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            database = _database_of(args, kwargs)
            if not _is_in_memory(database):
                opened.append((str(database), connection))
            return connection

        suite = unittest.TestLoader().loadTestsFromModule(module)  # type: ignore[arg-type]
        result = unittest.TestResult()
        with mock.patch.object(sqlite3, "connect", tracing_connect):
            suite.run(result)

        # The inner tests are the fixtures here: if they broke, the leak check
        # would be reading a run that never reached the database code.
        self.assertTrue(
            result.wasSuccessful(),
            "tests/test_legacy_cleanup.py must pass before its handles can be "
            f"checked; {len(result.failures) + len(result.errors)} of "
            f"{result.testsRun} failed:\n"
            + "\n".join(f"{name}: {detail}" for name, detail in result.failures + result.errors),
        )
        self.assertTrue(opened, "no file-backed database was opened; the check proved nothing")

        leaked = sorted({path for path, connection in opened if _is_open(connection)})
        self.assertEqual(
            leaked,
            [],
            "these database files were still open when their test finished, "
            "which fails TemporaryDirectory cleanup on Windows (WinError 32): "
            + ", ".join(leaked)
            + " — wrap the connection in contextlib.closing(...)",
        )


if __name__ == "__main__":
    unittest.main()
