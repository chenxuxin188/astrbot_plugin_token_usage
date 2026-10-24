"""按「日期 × 会话 × 模型」累计 token 用量的 SQLite 账本（不依赖 AstrBot，便于单独测试）"""

import asyncio
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_usage (
    day          TEXT    NOT NULL,
    umo          TEXT    NOT NULL,
    model        TEXT    NOT NULL DEFAULT '',
    rounds       INTEGER NOT NULL DEFAULT 0,
    input_other  INTEGER NOT NULL DEFAULT 0,
    input_cached INTEGER NOT NULL DEFAULT 0,
    output       INTEGER NOT NULL DEFAULT 0,
    updated_at   REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (day, umo, model)
);
CREATE INDEX IF NOT EXISTS idx_daily_usage_umo ON daily_usage (umo, day);
"""

SUM_COLUMNS = (
    "COALESCE(SUM(rounds), 0), COALESCE(SUM(input_other), 0), "
    "COALESCE(SUM(input_cached), 0), COALESCE(SUM(output), 0)"
)


@dataclass
class Usage:
    rounds: int = 0
    """记录到的对话轮数（一次 Agent 运行算一轮，含其中的工具调用步骤）"""
    input_other: int = 0
    """未命中缓存的输入 token"""
    input_cached: int = 0
    """命中缓存的输入 token"""
    output: int = 0
    """输出 token（含思考内容）"""

    @property
    def input(self) -> int:
        return self.input_other + self.input_cached

    @property
    def total(self) -> int:
        return self.input + self.output

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.rounds + other.rounds,
            self.input_other + other.input_other,
            self.input_cached + other.input_cached,
            self.output + other.output,
        )

    def __sub__(self, other: "Usage") -> "Usage":
        return Usage(
            max(0, self.rounds - other.rounds),
            max(0, self.input_other - other.input_other),
            max(0, self.input_cached - other.input_cached),
            max(0, self.output - other.output),
        )

    @classmethod
    def from_row(cls, row) -> "Usage":
        return cls(*(int(v or 0) for v in row))


class UsageStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ 写入

    async def add(self, day: str, umo: str, model: str, usage: Usage):
        await asyncio.to_thread(self._add, day, umo, model or "", usage)

    def _add(self, day: str, umo: str, model: str, usage: Usage):
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO daily_usage (day, umo, model, rounds, input_other, input_cached, output, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (day, umo, model) DO UPDATE SET
                    rounds       = rounds + excluded.rounds,
                    input_other  = input_other + excluded.input_other,
                    input_cached = input_cached + excluded.input_cached,
                    output       = output + excluded.output,
                    updated_at   = excluded.updated_at
                """,
                (day, umo, model, usage.rounds, usage.input_other, usage.input_cached,
                 usage.output, time.time()),
            )
            self._conn.commit()

    async def prune(self, today: str, keep_days: int) -> int:
        """删除 keep_days 天之前的记录（含今天在内保留 keep_days 天），keep_days <= 0 表示永久保留"""
        if keep_days <= 0:
            return 0
        keep_days = min(keep_days, 36500)  # 过大的值会让日期减法溢出
        cutoff = (date.fromisoformat(today) - timedelta(days=keep_days - 1)).isoformat()
        return await asyncio.to_thread(self._prune, cutoff)

    def _prune(self, cutoff: str) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM daily_usage WHERE day < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------ 查询

    async def session_usage(self, day: str, umo: str) -> Usage:
        return await asyncio.to_thread(self._session_usage, day, umo)

    def _session_usage(self, day: str, umo: str) -> Usage:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {SUM_COLUMNS} FROM daily_usage WHERE day = ? AND umo = ?",
                (day, umo),
            ).fetchone()
        return Usage.from_row(row)

    async def day_usage(self, day: str) -> tuple[Usage, int]:
        """返回 (当天所有会话的合计, 当天有消耗的会话数)"""
        return await asyncio.to_thread(self._day_usage, day)

    def _day_usage(self, day: str) -> tuple[Usage, int]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {SUM_COLUMNS}, COUNT(DISTINCT umo) FROM daily_usage WHERE day = ?",
                (day,),
            ).fetchone()
        return Usage.from_row(row[:4]), int(row[4] or 0)

    async def day_models(self, day: str) -> list[tuple[str, Usage]]:
        """当天按模型拆分的用量，按总量从高到低"""
        return await asyncio.to_thread(self._group_by, "model", day, None)

    async def top_sessions(self, day: str, limit: int = 10) -> list[tuple[str, Usage]]:
        """当天消耗最多的会话，按总量从高到低"""
        return await asyncio.to_thread(self._group_by, "umo", day, limit)

    def _group_by(self, column: str, day: str, limit: int | None) -> list[tuple[str, Usage]]:
        sql = (
            f"SELECT {column}, {SUM_COLUMNS} FROM daily_usage WHERE day = ? GROUP BY {column} "
            "ORDER BY SUM(input_other) + SUM(input_cached) + SUM(output) DESC"
        )
        params: tuple = (day,)
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [(row[0], Usage.from_row(row[1:])) for row in rows]
