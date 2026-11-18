"""单一职责：派生稳定主键。文章记录优先使用 identity 判定的文章身份。"""
import hashlib, json

def key_of(record):
    art = record.get("article_id")
    if art:
        return str(art)
    rid = record.get("id")
    if rid:
        return str(rid)
    return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
