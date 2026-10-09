"""Single-threaded SQLite executor used by the persistence adapter."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from threading import Lock, Thread
from typing import TypeVar, cast

from voiceink_win.domain.persistence import PersistenceClosedError

Result = TypeVar("Result")
Startup = Callable[[sqlite3.Connection], None]


@dataclass(slots=True)
class _Task:
    operation: Callable[[sqlite3.Connection], object]
    future: Future[object]
    transaction: bool


@dataclass(slots=True)
class _Stop:
    future: Future[object]


class SerializedSQLiteExecutor:
    """Own one connection and serialize every operation on one daemon thread."""

    def __init__(
        self,
        database_path: Path,
        startup: Startup,
        *,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must not be negative")
        self._database_path = Path(database_path)
        self._startup = startup
        self._busy_timeout_ms = busy_timeout_ms
        self._queue: Queue[_Task | _Stop] = Queue()
        self._lock = Lock()
        self._closing = False
        self._failure: BaseException | None = None
        self._terminated = False
        self._close_future: Future[None] | None = None
        self._ready_future: Future[None] = Future()
        self._thread = Thread(target=self._run, name="voiceink-sqlite-worker", daemon=True)
        self._thread.start()

    def ready(self) -> Future[None]:
        return self._ready_future

    def submit(
        self,
        operation: Callable[[sqlite3.Connection], Result],
        *,
        transaction: bool = False,
    ) -> Future[Result]:
        future: Future[Result] = Future()
        with self._lock:
            if self._failure is not None:
                future.set_exception(self._failure)
                return future
            if self._closing:
                future.set_exception(PersistenceClosedError("SQLite persistence is closed"))
                return future
            self._queue.put(_Task(operation, cast(Future[object], future), transaction))
        return future

    def close(self) -> Future[None]:
        with self._lock:
            if self._close_future is not None:
                return self._close_future
            self._closing = True
            close_future: Future[None] = Future()
            self._close_future = close_future
            if self._terminated:
                close_future.set_result(None)
                return close_future
            self._queue.put(_Stop(cast(Future[object], close_future)))
            return close_future

    def _run(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            self._database_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self._database_path, timeout=self._busy_timeout_ms / 1000)
            connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA foreign_keys = ON")
            self._startup(connection)
            self._set_result_once(self._ready_future, None)
        except BaseException as error:
            with self._lock:
                self._failure = error
                self._terminated = True
            if connection is not None:
                connection.close()
            self._set_exception_once(self._ready_future, error)
            self._fail_queued(error)
            with self._lock:
                if self._close_future is not None and not self._close_future.done():
                    self._close_future.set_result(None)
            return

        assert connection is not None
        while True:
            item = self._queue.get()
            if isinstance(item, _Stop):
                connection.close()
                self._set_result_once(item.future, None)
                with self._lock:
                    self._terminated = True
                return
            try:
                if item.transaction:
                    connection.execute("BEGIN IMMEDIATE")
                result = item.operation(connection)
                if item.transaction:
                    connection.commit()
            except BaseException as error:
                if item.transaction:
                    connection.rollback()
                self._set_exception_once(item.future, error)
            else:
                self._set_result_once(item.future, result)

    def _fail_queued(self, error: BaseException) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except Empty:
                return
            if isinstance(item, _Stop):
                self._set_result_once(item.future, None)
            else:
                self._set_exception_once(item.future, error)

    @staticmethod
    def _set_result_once(future: Future[object], result: object) -> None:
        if not future.done():
            future.set_result(result)

    @staticmethod
    def _set_exception_once(future: Future[object], error: BaseException) -> None:
        if not future.done():
            future.set_exception(error)
