"""人工纠偏存储：以详情页内容 SHA-256 为条件，在 SQLite 保存核正记录。

一条纠偏记录只对“某个 URL 的某一份具体内容”负责：
- 主键为 (detail_url, content_hash)，content_hash 是提交时详情正文的
  SHA-256；重新采集时内容哈希一致才套用核正字段；
- 同一 URL 内容变化后哈希不同，旧记录不会被新内容命中，即旧纠偏自动失效，
  该地址转入待复核，而不是继续套用过期字段或退回未经复核的自动解析。
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS corrections (
    detail_url       TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    road_id          TEXT NOT NULL,
    direction        TEXT NOT NULL,
    effective_start  TEXT NOT NULL,
    effective_end    TEXT,
    weekdays         TEXT NOT NULL,
    clock_start      TEXT NOT NULL,
    clock_end        TEXT NOT NULL,
    cross_midnight   INTEGER NOT NULL,
    title            TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (detail_url, content_hash)
)
"""


@dataclass(frozen=True)
class CorrectionRecord:
    """一条经人工核正的封闭字段记录（绑定具体内容哈希）。"""

    detail_url: str
    content_hash: str
    road_id: str
    direction: str
    effective_start: date
    effective_end: Optional[date]
    weekdays: list[int]
    clock_start: time
    clock_end: time
    cross_midnight: bool
    title: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


def default_db_path() -> Path:
    """纠偏库位置：优先环境变量，否则用工作目录下 data/corrections.db。"""
    return Path(os.environ.get("CORRECTION_DB_PATH", "data/corrections.db"))


class CorrectionStore:
    """SQLite 纠偏记录读写（短连接，线程安全由调用方串行化保证）。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(_SCHEMA)

    def upsert(self, record: CorrectionRecord) -> CorrectionRecord:
        """按 (url, hash) 写入或更新；同一内容重复提交时保留 created_at。"""
        now = datetime.utcnow()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT created_at FROM corrections "
                "WHERE detail_url = ? AND content_hash = ?",
                (record.detail_url, record.content_hash),
            ).fetchone()
            created_at = existing["created_at"] if existing else now.isoformat(timespec="seconds")
            conn.execute(
                """
                INSERT INTO corrections (
                    detail_url, content_hash, road_id, direction,
                    effective_start, effective_end, weekdays,
                    clock_start, clock_end, cross_midnight, title,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(detail_url, content_hash) DO UPDATE SET
                    road_id = excluded.road_id,
                    direction = excluded.direction,
                    effective_start = excluded.effective_start,
                    effective_end = excluded.effective_end,
                    weekdays = excluded.weekdays,
                    clock_start = excluded.clock_start,
                    clock_end = excluded.clock_end,
                    cross_midnight = excluded.cross_midnight,
                    title = excluded.title,
                    updated_at = excluded.updated_at
                """,
                (
                    record.detail_url,
                    record.content_hash,
                    record.road_id,
                    record.direction,
                    record.effective_start.isoformat(),
                    record.effective_end.isoformat() if record.effective_end else None,
                    json.dumps(record.weekdays, ensure_ascii=False),
                    record.clock_start.isoformat(),
                    record.clock_end.isoformat(),
                    1 if record.cross_midnight else 0,
                    record.title,
                    created_at,
                    now.isoformat(timespec="seconds"),
                ),
            )
        return CorrectionRecord(
            **{**record.__dict__, "created_at": datetime.fromisoformat(created_at),
               "updated_at": now.replace(microsecond=0)}
        )

    def get(self, detail_url: str, content_hash: str) -> Optional[CorrectionRecord]:
        """精确匹配 URL + 当前内容哈希；内容变化后返回 None（旧纠偏失效）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM corrections WHERE detail_url = ? AND content_hash = ?",
                (detail_url, content_hash),
            ).fetchone()
        return _row_to_record(row) if row else None

    def any_for_url(self, detail_url: str) -> bool:
        """该 URL 是否存在任何（可能已过期的）纠偏记录。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM corrections WHERE detail_url = ? LIMIT 1",
                (detail_url,),
            ).fetchone()
        return row is not None

    def list_for_url(self, detail_url: str) -> list[CorrectionRecord]:
        """列出某 URL 的全部内容版本纠偏记录（新内容在前）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM corrections WHERE detail_url = ? "
                "ORDER BY updated_at DESC",
                (detail_url,),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def delete_for_url(self, detail_url: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM corrections WHERE detail_url = ?", (detail_url,)
            )
            return cur.rowcount


def _row_to_record(row: sqlite3.Row) -> CorrectionRecord:
    return CorrectionRecord(
        detail_url=row["detail_url"],
        content_hash=row["content_hash"],
        road_id=row["road_id"],
        direction=row["direction"],
        effective_start=date.fromisoformat(row["effective_start"]),
        effective_end=date.fromisoformat(row["effective_end"]) if row["effective_end"] else None,
        weekdays=json.loads(row["weekdays"]),
        clock_start=time.fromisoformat(row["clock_start"]),
        clock_end=time.fromisoformat(row["clock_end"]),
        cross_midnight=bool(row["cross_midnight"]),
        title=row["title"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


_store: Optional[CorrectionStore] = None


def get_store() -> CorrectionStore:
    """进程内单例，数据库路径由 CORRECTION_DB_PATH 控制（便于测试隔离）。"""
    global _store
    if _store is None:
        _store = CorrectionStore()
    return _store


def reset_store(db_path: str | Path | None = None) -> CorrectionStore:
    """重置单例（主要给测试与启动装配使用）。"""
    global _store
    _store = CorrectionStore(db_path)
    return _store
