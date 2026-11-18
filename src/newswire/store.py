"""单一职责：本地 sqlite3 持久化。

存储分四层，全部围绕“文章”而不是“输入记录”组织：

``records(key, data)``
    当前文章文档（含版本链、来源集合、关系事件）。``key`` 是文章稳定键。
``aliases(alias, key)``
    身份别名 → 文章键。别名来自 ``identity`` 阶段；先到先得，保证旧批次
    回放时同一别名始终指回最初的文章。
``versions(version_key, key, content_digest, data)``
    不可变的内容版本。``version_key = 文章键 + 内容摘要``，因此重入、
    部分写入后重跑、旧批次回放都不会重复追加版本。
``events(event_key, key, type, data)``
    转载/改版/撤稿/重发等关系事件，按自然键幂等写入。

``upsert`` 在单个事务里写入文章、别名、版本和事件；进程在第 N 条记录
后中断，已提交的记录是完整可恢复的，重跑会从剩余记录继续并收敛到同一
结果。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from .config import DB_PATH

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS records(key TEXT PRIMARY KEY, data TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS aliases(alias TEXT PRIMARY KEY, key TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_aliases_key ON aliases(key)",
    "CREATE TABLE IF NOT EXISTS versions("
    "version_key TEXT PRIMARY KEY, key TEXT NOT NULL, content_digest TEXT, data TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_versions_key ON versions(key)",
    "CREATE TABLE IF NOT EXISTS events("
    "event_key TEXT PRIMARY KEY, key TEXT NOT NULL, type TEXT, data TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_events_key ON events(key)",
)


def connect(path: Any = None) -> sqlite3.Connection:
    con = sqlite3.connect(str(path) if path is not None else str(DB_PATH), timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")
    for statement in _SCHEMA:
        con.execute(statement)
    con.commit()
    return con


def _natural_key(prefix: str, payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return prefix + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def _article_doc(record) -> dict[str, Any]:
    data = record.data if hasattr(record, "data") else record
    doc = data.get("article") if isinstance(data, dict) else None
    return doc if isinstance(doc, dict) else data


def upsert(con: sqlite3.Connection, record) -> None:
    """原子写入一篇文章及其别名/版本/事件（幂等）。"""
    key = str(record.key)
    data = record.data if hasattr(record, "data") else record
    doc = _article_doc(record)

    with con:  # 单条记录一个事务：部分写入只可能落在记录边界上
        con.execute(
            "INSERT INTO records(key,data) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
            (key, json.dumps(doc, ensure_ascii=False, sort_keys=True)),
        )

        aliases = []
        if isinstance(data, dict):
            aliases.extend(data.get("identity_aliases") or [])
        aliases.extend(doc.get("aliases") or [])
        aliases.extend(doc.get("identity_aliases") or [])
        for alias in aliases:
            alias = str(alias)
            if not alias:
                continue
            con.execute(
                "INSERT INTO aliases(alias,key) VALUES(?,?) ON CONFLICT(alias) DO NOTHING",
                (alias, key),
            )

        for version in doc.get("versions") or []:
            version_key = version.get("version_key") or _natural_key(
                key + ":v:", version.get("content_digest") or version
            )
            con.execute(
                "INSERT INTO versions(version_key,key,content_digest,data) VALUES(?,?,?,?) "
                "ON CONFLICT(version_key) DO NOTHING",
                (
                    version_key,
                    key,
                    version.get("content_digest", ""),
                    json.dumps(version, ensure_ascii=False, sort_keys=True),
                ),
            )

        for event in doc.get("events") or []:
            event_key = event.get("event_key") or _natural_key(
                key + ":e:", {k: v for k, v in event.items() if k != "event_key"}
            )
            con.execute(
                "INSERT INTO events(event_key,key,type,data) VALUES(?,?,?,?) "
                "ON CONFLICT(event_key) DO NOTHING",
                (
                    event_key,
                    key,
                    event.get("type", ""),
                    json.dumps(event, ensure_ascii=False, sort_keys=True),
                ),
            )


def commit(con: sqlite3.Connection) -> None:
    con.commit()


def reset(con: sqlite3.Connection) -> None:
    """清空本库全部表；用于从零重建同一批次（回放/基准测试）。"""
    with con:
        for table in ("events", "versions", "aliases", "records"):
            con.execute(f"DELETE FROM {table}")


def count(con: sqlite3.Connection) -> int:
    return con.execute("SELECT COUNT(*) FROM records").fetchone()[0]


def all_records(con: sqlite3.Connection):
    return [(k, json.loads(d)) for k, d in con.execute("SELECT key,data FROM records ORDER BY key")]


def all_aliases(con: sqlite3.Connection):
    return list(con.execute("SELECT alias,key FROM aliases ORDER BY alias"))


def all_versions(con: sqlite3.Connection):
    return [
        (vk, key, digest, json.loads(d))
        for vk, key, digest, d in con.execute(
            "SELECT version_key,key,content_digest,data FROM versions ORDER BY key,version_key"
        )
    ]


def all_events(con: sqlite3.Connection):
    return [
        (ek, key, type_, json.loads(d))
        for ek, key, type_, d in con.execute(
            "SELECT event_key,key,type,data FROM events ORDER BY key,event_key"
        )
    ]


def version_count(con: sqlite3.Connection, key: str | None = None) -> int:
    if key is None:
        return con.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
    return con.execute("SELECT COUNT(*) FROM versions WHERE key=?", (key,)).fetchone()[0]


def event_count(con: sqlite3.Connection, key: str | None = None, type_: str | None = None) -> int:
    sql = "SELECT COUNT(*) FROM events"
    clauses: list[str] = []
    params: list[Any] = []
    if key is not None:
        clauses.append("key=?")
        params.append(key)
    if type_ is not None:
        clauses.append("type=?")
        params.append(type_)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    return con.execute(sql, params).fetchone()[0]


def get_record(con: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    row = con.execute("SELECT data FROM records WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def close(con: sqlite3.Connection) -> None:
    con.close()
