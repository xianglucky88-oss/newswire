"""单一职责：页面补全 —— 按 url 抓取页面（离线回放 fixtures/pages.json），
把页面 meta 与正文中的时间线索写入 time_clues.page_meta / time_clues.body。

迟到补全：本轮抓不到页面时只标记 miss，不造时间；后续轮次页面可用时
再补线索，publish_time 在合并时据此升级采用值并留下 revisions。
"""
import json

from . import config

_cache = {}


def _pages():
    path = str(config.PAGES)
    if path not in _cache:
        try:
            with open(path, encoding="utf-8") as f:
                _cache[path] = json.load(f)
        except FileNotFoundError:
            _cache[path] = {}
    return _cache[path]


def process(record):
    rec = dict(record)
    url = rec.get("url")
    page = _pages().get(url) if url else None
    if not page:
        rec["_page_fill"] = "miss"
        return rec
    rec["_page_fill"] = "filled"
    rec["page"] = page

    clues = dict(rec.get("time_clues") or {})
    meta = page.get("meta") or {}
    page_meta = {}
    if meta.get("published"):
        page_meta["published"] = meta["published"]
    if meta.get("modified"):
        page_meta["modified"] = meta["modified"]
    if meta.get("tz"):
        page_meta["tz"] = meta["tz"]
    if page_meta:
        clues["page_meta"] = page_meta

    body = {}
    if page.get("body_date"):
        body["date"] = page["body_date"]
    if page.get("body_date_relative"):
        body["relative"] = page["body_date_relative"]
    if page.get("tz"):
        body["tz"] = page["tz"]
    if body:
        body["ref"] = rec.get("fetched_at")  # 相对时间的参照时刻
        clues["body"] = body
    if clues:
        rec["time_clues"] = clues
    return rec
