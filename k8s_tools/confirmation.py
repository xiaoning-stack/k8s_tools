import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Optional


DEFAULT_EXPIRY_SECONDS = 600


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(dt: datetime) -> str:
    return dt.isoformat()


def _connect(db_path: str) -> sqlite3.Connection:
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_operations (
            operation_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            summary TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            executed_at TEXT,
            result_text TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_pending_operations_session_status
        ON pending_operations(session_id, status)
        """
    )
    conn.commit()


def _mark_expired(conn: sqlite3.Connection) -> None:
    now = _to_iso(_utcnow())
    conn.execute(
        """
        UPDATE pending_operations
        SET status = 'expired'
        WHERE status = 'pending' AND expires_at < ?
        """,
        (now,),
    )
    conn.commit()


def create_pending_operation(
    db_path: str,
    session_id: str,
    kind: str,
    payload: Dict,
    summary: str,
    expires_in_seconds: int = DEFAULT_EXPIRY_SECONDS,
) -> str:
    operation_id = f"op_{uuid.uuid4().hex[:12]}"
    created_at = _utcnow()
    expires_at = created_at + timedelta(seconds=expires_in_seconds)

    with _connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO pending_operations (
                operation_id,
                session_id,
                kind,
                summary,
                payload_json,
                status,
                created_at,
                expires_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                operation_id,
                session_id,
                kind,
                summary,
                json.dumps(payload, ensure_ascii=False),
                _to_iso(created_at),
                _to_iso(expires_at),
            ),
        )
        conn.commit()

    return operation_id


def get_pending_operation(db_path: str, operation_id: str) -> Optional[Dict]:
    with _connect(db_path) as conn:
        _ensure_schema(conn)
        _mark_expired(conn)
        row = conn.execute(
            """
            SELECT *
            FROM pending_operations
            WHERE operation_id = ?
            """,
            (operation_id,),
        ).fetchone()

    if not row:
        return None

    return {
        "operation_id": row["operation_id"],
        "session_id": row["session_id"],
        "kind": row["kind"],
        "summary": row["summary"],
        "payload": json.loads(row["payload_json"]),
        "status": row["status"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "executed_at": row["executed_at"],
        "result_text": row["result_text"],
    }


def list_pending_operations(db_path: str, session_id: str) -> list[Dict]:
    with _connect(db_path) as conn:
        _ensure_schema(conn)
        _mark_expired(conn)
        rows = conn.execute(
            """
            SELECT operation_id, kind, summary, created_at, expires_at
            FROM pending_operations
            WHERE session_id = ? AND status = 'pending'
            ORDER BY created_at DESC
            """,
            (session_id,),
        ).fetchall()

    return [
        {
            "operation_id": row["operation_id"],
            "kind": row["kind"],
            "summary": row["summary"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
        }
        for row in rows
    ]


def cancel_pending_operation(db_path: str, session_id: str, operation_id: str) -> str:
    with _connect(db_path) as conn:
        _ensure_schema(conn)
        _mark_expired(conn)
        row = conn.execute(
            """
            SELECT status, summary
            FROM pending_operations
            WHERE operation_id = ? AND session_id = ?
            """,
            (operation_id, session_id),
        ).fetchone()

        if not row:
            return f"未找到待确认操作: {operation_id}"

        if row["status"] != "pending":
            return f"操作 {operation_id} 当前状态为 {row['status']}，不能取消"

        conn.execute(
            """
            UPDATE pending_operations
            SET status = 'cancelled'
            WHERE operation_id = ? AND session_id = ?
            """,
            (operation_id, session_id),
        )
        conn.commit()

    return f"已取消操作 {operation_id}\n摘要: {row['summary']}"


def execute_pending_operation(
    db_path: str,
    session_id: str,
    operation_id: str,
    handlers: Dict[str, Callable[..., str]],
) -> str:
    with _connect(db_path) as conn:
        _ensure_schema(conn)
        _mark_expired(conn)
        row = conn.execute(
            """
            SELECT *
            FROM pending_operations
            WHERE operation_id = ? AND session_id = ?
            """,
            (operation_id, session_id),
        ).fetchone()

        if not row:
            return f"未找到待确认操作: {operation_id}"

        if row["status"] != "pending":
            return f"操作 {operation_id} 当前状态为 {row['status']}，不能执行"

        updated = conn.execute(
            """
            UPDATE pending_operations
            SET status = 'executing'
            WHERE operation_id = ? AND session_id = ? AND status = 'pending'
            """,
            (operation_id, session_id),
        )
        conn.commit()

        if updated.rowcount != 1:
            return f"操作 {operation_id} 状态已变化，请重新查询"

        kind = row["kind"]
        payload = json.loads(row["payload_json"])

    handler = handlers.get(kind)
    if handler is None:
        result_text = f"未找到操作类型 {kind} 的执行器"
        final_status = "failed"
    else:
        try:
            result_text = handler(**payload)
            final_status = "executed"
        except Exception as exc:
            result_text = f"执行失败: {exc}"
            final_status = "failed"

    with _connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            UPDATE pending_operations
            SET status = ?, executed_at = ?, result_text = ?
            WHERE operation_id = ? AND session_id = ?
            """,
            (
                final_status,
                _to_iso(_utcnow()),
                result_text,
                operation_id,
                session_id,
            ),
        )
        conn.commit()

    return result_text


def clear_operations(db_path: str, session_id: Optional[str] = None) -> None:
    with _connect(db_path) as conn:
        _ensure_schema(conn)
        if session_id:
            conn.execute(
                "DELETE FROM pending_operations WHERE session_id = ?",
                (session_id,),
            )
        else:
            conn.execute("DELETE FROM pending_operations")
        conn.commit()
