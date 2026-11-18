"""单一职责：字段规整/规范化。规范阶段，不做时间解释。"""


def process(record):
    rec = dict(record)
    rec["src"] = str(rec.get("src", "")).strip().lower()
    for field in ("name", "title", "url", "guid", "fetched_at"):
        if field in rec:
            rec[field] = str(rec[field]).strip()
    if "value" in rec:
        rec["value"] = int(rec["value"])
    return rec
