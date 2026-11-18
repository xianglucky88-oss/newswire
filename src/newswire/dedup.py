"""单一职责：依据 identity 的文章身份标注重复输入。（作为流水线阶段）"""
def process(record):
    rec = dict(record)
    rec.setdefault("_stage_dedup", "ok")
    ident = rec.get("identity")
    if ident and ident.get("duplicate") and rec.get("article_id"):
        rec["dup_of"] = rec["article_id"]
    return rec
