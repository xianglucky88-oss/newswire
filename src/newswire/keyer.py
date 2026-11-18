"""单一职责：派生稳定主键。

优先级：

1. ``article_key`` / ``_article_key``：``dedup`` 已经判定文章身份时直接沿用；
2. ``article.key``：记录包裹的文章文档；
3. ``identity`` 证据推导的稳定文章键（规范 URL → 来源+外部 id → 内容摘要）；
4. 兼容旧数据：原始 ``id``；
5. 最后才是整条记录的摘要。

关键约束：键不能依赖批次、抓取时间或更新时间。同一篇文章无论从哪个
媒体、哪次回放进入，都必须落到同一键上；仅更新时间变化不能产生新键。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def _hash(text: str, length: int = 20) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def stable_article_key(identity: dict[str, Any] | None = None, *, record: dict[str, Any] | None = None) -> str:
    """由身份证据推导稳定文章键（不读取任何时间字段）。"""
    identity = identity or {}
    if record is not None and not identity:
        identity = record.get("identity") or {}

    url = identity.get("canonical_url") or identity.get("url") or ""
    if url:
        return "a-" + _hash("url:" + url)

    aliases = identity.get("aliases") or []
    for alias in aliases:
        text = str(alias)
        if ":" in text and not text.lower().startswith(("http://", "https://")):
            return "a-" + _hash("alias:" + text)

    digest = identity.get("content_digest") or identity.get("content_key") or ""
    if digest:
        return "a-" + _hash("content:" + str(digest))

    title = identity.get("title_norm") or ""
    if title:
        return "a-" + _hash("title:" + title)

    source = identity.get("source") or ""
    if record is not None and record.get("id") not in (None, ""):
        return "a-" + _hash(f"id:{source}:{record.get('id')}")
    if source and record is not None:
        return "a-" + _hash("source:" + str(source))
    return ""


def key_of(record: dict[str, Any]) -> str:
    for field in ("article_key", "_article_key"):
        value = record.get(field)
        if value:
            return str(value)
    article = record.get("article")
    if isinstance(article, dict) and article.get("key"):
        return str(article["key"])

    identity = record.get("identity")
    if isinstance(identity, dict) and identity:
        key = stable_article_key(identity, record=record)
        if key:
            return key

    rid = record.get("id")
    if rid:
        return str(rid)
    return hashlib.sha256(
        json.dumps(record, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()[:16]
