"""SQLite connection primitives and pool management."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator, Awaitable, Callable

import aiosqlite


DATETIME_COLUMNS = {
    "createdAt",
    "closedAt",
    "activeAt",
    "expireAt",
    "billingStart",
    "billingEnd",
    "startTime",
    "endTime",
}


def parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def row_to_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for key in DATETIME_COLUMNS:
        value = result.get(key)
        if isinstance(value, str):
            result[key] = parse_datetime(value)
    return result


def rows_to_dicts(rows: list[Any]) -> list[dict[str, Any]]:
    return [row_to_dict(row) for row in rows]


class ExecResult:
    def __init__(self, lastrowid: int | None = None):
        self.lastrowid = lastrowid


class DBConn:
    """SQLite wrapper with fetch/execute/transaction helpers."""

    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    @staticmethod
    def _params(params: tuple[Any, ...]) -> tuple[Any, ...]:
        return tuple(
            value.isoformat() if isinstance(value, datetime) else value
            for value in params
        )

    async def fetchrow(self, sql: str, *params: Any) -> dict[str, Any] | None:
        cursor = await self._conn.execute(sql, self._params(params))
        try:
            return row_to_dict(await cursor.fetchone())
        finally:
            await cursor.close()

    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(sql, self._params(params))
        try:
            return rows_to_dicts(await cursor.fetchall())
        finally:
            await cursor.close()

    async def fetchval(self, sql: str, *params: Any) -> Any:
        row = await self.fetchrow(sql, *params)
        if not row:
            return None
        return next(iter(row.values()))

    async def execute(self, sql: str, *params: Any) -> ExecResult:
        cursor = await self._conn.execute(sql, self._params(params))
        try:
            return ExecResult(cursor.lastrowid)
        finally:
            await cursor.close()

    @asynccontextmanager
    async def transaction(self, immediate: bool = False) -> AsyncIterator["DBConn"]:
        try:
            if immediate:
                await self._conn.execute("BEGIN IMMEDIATE")
            yield self
            await self._conn.commit()
        except BaseException:
            await self._conn.rollback()
            raise


Initializer = Callable[[DBConn], Awaitable[None]]


class SQLitePool:
    """Small fixed-size SQLite connection pool initialized once per service."""

    def __init__(self, db_path: str, size: int = 5):
        self.db_path = db_path
        self.size = max(1, int(size))
        self._queue: asyncio.Queue[DBConn] | None = None
        self._init_lock = asyncio.Lock()

    async def connect(self, initializer: Initializer) -> None:
        if self._queue is not None:
            return
        async with self._init_lock:
            if self._queue is not None:
                return
            directory = os.path.dirname(os.path.abspath(self.db_path))
            os.makedirs(directory, exist_ok=True)
            raw_connections: list[aiosqlite.Connection] = []
            try:
                for _ in range(self.size):
                    raw = await aiosqlite.connect(self.db_path)
                    raw.row_factory = sqlite3.Row
                    await raw.execute("PRAGMA journal_mode=WAL")
                    await raw.execute("PRAGMA foreign_keys=ON")
                    await raw.execute("PRAGMA busy_timeout=5000")
                    raw_connections.append(raw)
                wrapped = [DBConn(raw) for raw in raw_connections]
                await initializer(wrapped[0])
            except BaseException:
                for raw in raw_connections:
                    await raw.close()
                raise

            queue: asyncio.Queue[DBConn] = asyncio.Queue()
            for connection in wrapped:
                await queue.put(connection)
            self._queue = queue

    async def close(self) -> None:
        if self._queue is None:
            return
        while not self._queue.empty():
            connection = self._queue.get_nowait()
            await connection._conn.close()
        self._queue = None

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[DBConn]:
        if self._queue is None:
            raise RuntimeError("SQLite pool is not connected")
        connection = await self._queue.get()
        try:
            yield connection
        finally:
            await self._queue.put(connection)
