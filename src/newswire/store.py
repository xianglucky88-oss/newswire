"""单一职责：本地 sqlite3 持久化，提供幂等 upsert / 计数 / 全量读取。"""
import json, sqlite3
from .config import DB_PATH

def connect():
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS records(key TEXT PRIMARY KEY, data TEXT NOT NULL)")
    return con

def upsert(con, record):
    data = record.data
    # 身份归并产生的换键：删除被取代的旧主键行（幂等，键不存在时为 no-op）
    for old in data.get("supersedes") or []:
        if old != record.key:
            con.execute("DELETE FROM records WHERE key=?", (old,))
    con.execute("INSERT INTO records(key,data) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                (record.key, json.dumps(data, ensure_ascii=False)))

def commit(con): con.commit()

def count(con): return con.execute("SELECT COUNT(*) FROM records").fetchone()[0]

def all_records(con):
    return [(k, json.loads(d)) for k, d in con.execute("SELECT key,data FROM records")]
