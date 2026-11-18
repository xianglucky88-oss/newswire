"""单一职责：派生稳定主键。优先文章身份（与时间无关），再 id，最后内容哈希。"""
import hashlib, json


def key_of(record):
    if record.get("article_key"):
        return str(record["article_key"])
    if record.get("id"):
        return str(record["id"])
    return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
