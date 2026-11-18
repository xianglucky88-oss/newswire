"""单一职责：把 RSS 条目中的时间线索提取为 time_clues.rss（只取线索，不做解释）。

输出契约：time_clues.rss = {published, updated, seen_at}（缺项不补）。
published/updated 的取舍与置信判断由 publish_time 阶段统一完成。
"""


def process(record):
    rec = dict(record)
    clues = dict(rec.get("time_clues") or {})
    rss = {}
    for field in ("rss_pubdate", "pubdate", "pubDate", "published"):
        if rec.get(field):
            rss.setdefault("published", rec[field])
    if rec.get("rss_updated"):
        rss["updated"] = rec["rss_updated"]
    if rec.get("fetched_at"):
        rss["seen_at"] = rec["fetched_at"]
    if rss:
        clues["rss"] = rss
    if clues:
        rec["time_clues"] = clues
    return rec
