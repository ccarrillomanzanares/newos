"""Memoria episódica: historial persistente de conversaciones en SQLite."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger("agentos.memory.episodic")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    session_id TEXT,
    role TEXT,          -- 'user', 'assistant', 'tool'
    content TEXT,
    tool_name TEXT,
    tool_result TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
"""

VALID_ROLES = {"user", "assistant", "tool"}


def new_session_id() -> str:
    return uuid.uuid4().hex[:12]


class EpisodicMemory:
    """Acceso asíncrono (vía hilo) a la base de datos SQLite de mensajes."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------- conexión
    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            if str(self.db_path) != ":memory:":
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.commit()
            self._conn = conn
            logger.debug("Base de datos episódica abierta", extra={"db": str(self.db_path)})
        return self._conn

    def _run(self, sql: str, params: tuple = (), fetch: bool = False) -> Any:
        with self._lock:
            conn = self._connect()
            cur = conn.execute(sql, params)
            if fetch:
                return [dict(r) for r in cur.fetchall()]
            conn.commit()
            return cur.lastrowid

    async def init(self) -> None:
        await asyncio.to_thread(self._connect)

    async def close(self) -> None:
        def _close() -> None:
            with self._lock:
                if self._conn is not None:
                    self._conn.close()
                    self._conn = None
        await asyncio.to_thread(_close)

    # --------------------------------------------------------------- API
    async def add_message(self, session_id: str, role: str, content: str, tool_name: str | None = None,
                          tool_result: str | None = None) -> int:
        """Guarda un mensaje y devuelve su id."""
        if role not in VALID_ROLES:
            raise ValueError(f"Rol inválido: {role}")
        return await asyncio.to_thread(
            self._run,
            "INSERT INTO messages (session_id, role, content, tool_name, tool_result) VALUES (?, ?, ?, ?, ?)",
            (session_id, role, content, tool_name, tool_result),
        )

    async def get_session_history(self, session_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        """Mensajes de una sesión en orden cronológico (los últimos ``limit`` si se indica)."""
        if limit:
            rows = await asyncio.to_thread(
                self._run,
                "SELECT * FROM (SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id",
                (session_id, int(limit)), True,
            )
        else:
            rows = await asyncio.to_thread(
                self._run, "SELECT * FROM messages WHERE session_id = ? ORDER BY id", (session_id,), True)
        return rows

    async def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        """Sesiones ordenadas por actividad reciente, con primer mensaje del usuario como título."""
        return await asyncio.to_thread(
            self._run,
            """
            SELECT m.session_id,
                   MIN(m.timestamp) AS started,
                   MAX(m.timestamp) AS last_activity,
                   COUNT(*) AS message_count,
                   (SELECT content FROM messages u WHERE u.session_id = m.session_id AND u.role = 'user'
                    ORDER BY u.id LIMIT 1) AS title
            FROM messages m
            GROUP BY m.session_id
            ORDER BY MAX(m.id) DESC
            LIMIT ?
            """,
            (int(limit),), True,
        )

    async def delete_session(self, session_id: str) -> None:
        await asyncio.to_thread(self._run, "DELETE FROM messages WHERE session_id = ?", (session_id,))

    async def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Búsqueda simple por subcadena en el historial."""
        return await asyncio.to_thread(
            self._run, "SELECT * FROM messages WHERE content LIKE ? ORDER BY id DESC LIMIT ?",
            (f"%{query}%", int(limit)), True,
        )
