"""单一职责：本地 sqlite3 持久化，提供幂等 upsert / 计数 / 全量读取。

upsert 支持可选 merge(old_data, new_data)：键冲突时先合并再写入，
合并策略由业务阶段提供（编排层只负责接线）。
"""
import json, sqlite3
from . import config


def connect(path=None):
    con = sqlite3.connect(str(path or config.DB_PATH))
    con.execute("CREATE TABLE IF NOT EXISTS records(key TEXT PRIMARY KEY, data TEXT NOT NULL)")
    return con


def upsert(con, record, merge=None):
    data = record.data
    if merge is not None:
        row = con.execute("SELECT data FROM records WHERE key=?", (record.key,)).fetchone()
        if row:
            data = merge(json.loads(row[0]), data)
    con.execute("INSERT INTO records(key,data) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                (record.key, json.dumps(data, ensure_ascii=False)))


def get(con, key):
    row = con.execute("SELECT data FROM records WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def commit(con): con.commit()

def count(con): return con.execute("SELECT COUNT(*) FROM records").fetchone()[0]

def all_records(con):
    return [(k, json.loads(d)) for k, d in con.execute("SELECT key,data FROM records")]
