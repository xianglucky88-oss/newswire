"""发布时间证据：解析、规整并标记“这到底是不是发布时间”。

输入记录里可能有多个时间字段，含义并不相同：

``published``/``pubDate``/``datePublished``/``first_published``
    文章最初发布时间。转载一般会保留它，或改写为转载时间。

``updated``/``lastmod``/``dateModified``/``modified``
    媒体最后一次改动时间。**更新时间变化不代表产生了一篇新文章**，
    因此本阶段把发布时间和更新时间分开保存，并给出稳定的 ``first_seen``。

处理规则：

* 统一解析成带时区的 ISO-8601 UTC 文本（``published_at`` / ``updated_at``）；
* 同一个值出现在多个字段时只保留一次；
* 记录级时间证据包含 ``published_at``、``published_window``（发布区间）、
  ``updated_at`` 与 ``first_seen``，供 ``dedup`` 判断“转载 vs 改版 vs 撤稿重发”。
* 不改写调用方已经给出的规范化字段，方便旧批次回放。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

STAGE = "publish_time"

_PUBLISHED_FIELDS = (
    "published_at",
    "published",
    "pubdate",
    "pub_date",
    "publish_date",
    "publish_time",
    "published_time",
    "datepublished",
    "date_published",
    "first_published",
    "first_published_at",
    "original_published_at",
    "release_date",
    "issued",
    "created_at",
)
_UPDATED_FIELDS = (
    "updated_at",
    "updated",
    "last_updated",
    "lastmod",
    "last_modified",
    "modified",
    "date_modified",
    "datemodified",
    "dateupdated",
    "revised_at",
)
_OTHER_FIELDS = ("ts", "timestamp", "time", "date", "fetched_at", "retrieved_at", "ingested_at", "seen_at")

#: 解析出的时间若带时区则转 UTC；没有时区按 UTC 解释（上游多为 Z）。
_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%a, %d %b %Y %H:%M:%S %z",
    "%a, %d %b %Y %H:%M:%S %Z",
    "%a, %d %b %Y %H:%M %z",
    "%d %b %Y %H:%M:%S %z",
    "%d %b %Y %H:%M %z",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d",
)

#: 内容近似但发布时间不同时，仍视为同一篇稿件（转载/撤稿重发）的时间窗。
#: 24 小时覆盖常见的跨时区、跨日转载；超过后按“独立新文”处理，避免把
#: 长期跟踪报道吸成一篇。可由调用方通过 ``window_hours`` 覆盖。
DEFAULT_WINDOW_HOURS = 24
MAX_WINDOW_HOURS = 72


def parse_time(value: Any) -> datetime | None:
    """把常见时间文本解析成带 UTC 时区的 datetime；失败返回 ``None``。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 10_000_000_000:  # 毫秒时间戳
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+0000"
    if text.isdigit():
        return parse_time(int(text))

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in _FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def to_iso(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _first_parsed(record: dict[str, Any], fields: tuple[str, ...]) -> tuple[str, datetime | None, str]:
    """返回 (字段名, 解析结果, 原始值)；按字段优先级取第一个可解析值。"""
    fallback: tuple[str, datetime | None, str] = ("", None, "")
    for field in fields:
        for key, raw in record.items():
            if str(key).lower() != field:
                continue
            parsed = parse_time(raw)
            if parsed is not None:
                return str(key), parsed, str(raw)
            if not fallback[0] and raw not in (None, ""):
                fallback = (str(key), None, str(raw))
    return fallback


def collect_time_evidence(record: dict[str, Any], *, window_hours: float = DEFAULT_WINDOW_HOURS) -> dict[str, Any]:
    """从记录中收集时间证据；不改动记录。"""
    pub_field, published, pub_raw = _first_parsed(record, _PUBLISHED_FIELDS)
    upd_field, updated, upd_raw = _first_parsed(record, _UPDATED_FIELDS)
    other_field, other, other_raw = _first_parsed(record, _OTHER_FIELDS)

    # 上游只给了一个含糊的 ts/date 时，把它当作发布时间；同时标记来源，
    # 避免 dedup 把“抓取时间”误读为“发布时间”。
    published_source = pub_field
    published_kind = "declared" if pub_field else ""
    if published is None and other is not None:
        published, published_source = other, other_field or "other"
        # ts/fetched_at 通常是抓取时间而非媒体声明的发布时间；dedup 不会
        # 用“推断时间”把两条内容一致的记录拆成两篇。
        published_kind = "inferred"

    # “更新时间早于发布时间” 是明显的脏数据；保留证据但不要因此让
    # 时间区间为空。更新时间在发布时间之前时视为不可信的更新时间。
    if updated is not None and published is not None and updated < published:
        updated = published

    window = max(0.0, min(float(window_hours), MAX_WINDOW_HOURS))
    delta = timedelta(hours=window)
    if published is not None:
        window_start = published - delta
        window_end = published + delta
    else:
        window_start = window_end = None

    return {
        "published_at": to_iso(published),
        "published_at_raw": pub_raw if pub_field else other_raw,
        "published_field": published_source,
        "published_kind": published_kind,
        "updated_at": to_iso(updated),
        "updated_at_raw": upd_raw,
        "updated_field": upd_field,
        "first_seen": to_iso(other) if other_field else "",
        "first_seen_field": other_field,
        "window_hours": window,
        "published_window_start": to_iso(window_start),
        "published_window_end": to_iso(window_end),
        # 便于比较的 epoch 秒；None 表示没有可用的发布时间。
        "published_epoch": int(published.timestamp()) if published else None,
        "window_start_epoch": int(window_start.timestamp()) if window_start else None,
        "window_end_epoch": int(window_end.timestamp()) if window_end else None,
    }


def process(record: dict[str, Any]) -> dict[str, Any]:
    """流水线阶段：在记录上附加时间证据。"""
    rec = dict(record)
    evidence = collect_time_evidence(rec)
    rec["time_evidence"] = evidence
    for key in ("published_at", "updated_at", "first_seen"):
        if evidence[key]:
            rec[key] = evidence[key]
    rec["published_window"] = {
        "start": evidence["published_window_start"],
        "end": evidence["published_window_end"],
        "hours": evidence["window_hours"],
    }
    rec["_stage_publish_time"] = "ok"
    return rec
