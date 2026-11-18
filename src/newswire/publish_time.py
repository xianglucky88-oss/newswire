"""单一职责：还原发布时间，并输出候选、采用值与置信依据。

上游契约（各阶段只提供线索，不做时间判断）：
  time_clues.rss        —— rss_pull 从 RSS 条目提取（published / updated / seen_at）
  time_clues.page_meta  —— page_fill 从页面 meta 提取（published / modified / tz）
  time_clues.body       —— page_fill 从正文提取（date / relative / tz / ref）
  旧字段 ts / date / published_at / updated / modified —— 按 legacy 线索兼容读取

输出契约（identity / dedup / 列表排序共用同一份解释）：
  published_at   —— 采用的发布时间（UTC ISO8601 或 None；绝不回填处理当天）
  revised_at     —— 最近修订时间（与发布时间严格区分，修订不参与"最新"排序）
  publish_time   —— {published_at, revised_at, source, kind, precision,
                     confidence, basis, basis_code, candidates, revisions}

三条铁律：
  1. 可确定的绝对时间 > 相对时间 > 无法还原（None）；缺失时不拿处理当天冒充。
  2. 修订时间（updated/modified）永远不作为 published_at，只进 revised_at，
     避免旧新闻因被编辑而挤到最新位置。
  3. 同一记录同样的输入永远得到同样的输出（纯函数），重复抓取/崩溃重跑后日期稳定。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

try:  # 命名时区（如 America/New_York）；无 tzdata 时降级为 UTC 假设
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

# ---- 置信模型（可解释、可调，全部记录在每个候选上） ----
# 来源基础置信度：页面 meta 明确标注 > RSS 线索 > 正文日期 > 旧字段（来源不明）
SOURCE_CONFIDENCE = {"page_meta": 0.95, "rss": 0.85, "body": 0.70, "legacy": 0.60}
# 同置信度时的来源优先级（数值大者优先）
SOURCE_PRIORITY = {"page_meta": 3, "rss": 2, "body": 1, "legacy": 0}
# 类型系数：绝对时间原值，相对时间为估计值打折
KIND_FACTOR = {"absolute": 1.0, "relative": 0.7}
# 裸日期时间没有时区时的惩罚（按 UTC 假设处理并留 note）
TZ_ASSUMED_FACTOR = 0.9
# 绝对时间候选之间超过该差距视为日期冲突（同一瞬时的不同时区表达不算冲突）
CONFLICT_TOLERANCE = timedelta(hours=2)

UTC = timezone.utc

# ---- 解析：绝对时间（ISO8601 / RFC822 / 常见日期格式）与相对时间 ----
_STRPTIME_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%Y年%m月%d日", "%Y/%m/%d")

_RELATIVE_PATTERNS = (
    (re.compile(r"^(\d+)\s*(?:minutes?|mins?)\s+ago$", re.I), "minutes", "second"),
    (re.compile(r"^(\d+)\s*(?:hours?|hrs?)\s+ago$", re.I), "hours", "second"),
    (re.compile(r"^(\d+)\s*days?\s+ago$", re.I), "days", "day"),
    (re.compile(r"^(\d+)\s*weeks?\s+ago$", re.I), "weeks", "day"),
    (re.compile(r"^(\d+)\s*分钟前$"), "minutes", "second"),
    (re.compile(r"^(\d+)\s*小时前$"), "hours", "second"),
    (re.compile(r"^(\d+)\s*天前$"), "days", "day"),
    (re.compile(r"^(\d+)\s*周前$"), "weeks", "day"),
)
_RELATIVE_WORDS = {
    "yesterday": (timedelta(days=1), "day"),
    "today": (timedelta(0), "day"),
    "昨天": (timedelta(days=1), "day"),
    "今天": (timedelta(0), "day"),
}


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _to_epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def _localize(dt: datetime, tz_hint, note):
    """裸时间 → 带时区。有 tz 线索用线索（命名时区缺失时降级），否则按 UTC 假设。"""
    if dt.tzinfo is not None:
        return dt, False, note
    if tz_hint and ZoneInfo is not None:
        try:
            return dt.replace(tzinfo=ZoneInfo(str(tz_hint))), False, note
        except Exception:
            note = (note + "; " if note else "") + f"tz-unknown:{tz_hint}; tz-assumed-utc"
            return dt.replace(tzinfo=UTC), True, note
    note = (note + "; " if note else "") + "tz-assumed-utc"
    return dt.replace(tzinfo=UTC), True, note


def parse_time(raw, tz_hint=None, ref=None) -> dict:
    """把任意原始时间串解析为统一结构。

    返回 {value, kind, precision, tz_assumed, note}：
    value 为 UTC ISO8601 或 None（解析失败 / 相对时间缺参照）。
    """
    out = {"value": None, "kind": "absolute", "precision": "second",
           "tz_assumed": False, "note": ""}
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        out["note"] = "empty"
        return out

    # --- 相对时间：需要参照时刻（抓取时刻），没有参照则无法还原 ---
    if isinstance(raw, str):
        text = raw.strip()
        delta = precision = None
        for pattern, unit, prec in _RELATIVE_PATTERNS:
            m = pattern.match(text)
            if m:
                delta, precision = timedelta(**{unit: int(m.group(1))}), prec
                break
        if delta is None and text.lower() in _RELATIVE_WORDS:
            delta, precision = _RELATIVE_WORDS[text.lower()]
        if delta is not None:
            out["kind"] = "relative"
            out["precision"] = precision
            ref_dt = parse_time(ref)["value"] if isinstance(ref, str) else None
            if isinstance(ref, datetime):
                ref_dt = _iso(ref)
            if ref_dt is None:
                out["note"] = "relative-without-reference"
                return out
            base = datetime.fromisoformat(ref_dt)
            out["value"] = _iso(base - delta)
            out["note"] = f"relative-to:{ref_dt}"
            return out

    # --- 绝对时间 ---
    dt = None
    if isinstance(raw, datetime):
        dt = raw
    elif isinstance(raw, str):
        text = raw.strip()
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            pass
        if dt is None:
            try:
                dt = parsedate_to_datetime(text)  # RFC 822（RSS pubDate）
            except (TypeError, ValueError):
                pass
        if dt is None:
            for fmt in _STRPTIME_FORMATS:
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
        if dt is not None and dt.hour == 0 and dt.minute == 0 and dt.second == 0 \
                and not re.search(r"[T ]\d{2}:\d{2}", text):
            out["precision"] = "day"
            out["note"] = "date-only"
    if dt is None:
        out["note"] = (out["note"] + "; " if out["note"] else "") + "unparseable"
        return out
    dt, assumed, note = _localize(dt, tz_hint, out["note"])
    out["tz_assumed"] = assumed
    out["note"] = note
    out["value"] = _iso(dt)
    return out


# ---- 候选收集：时间线索 + 旧字段兼容 ----
def _candidate(source, role, raw, tz_hint=None, ref=None, extra_note=""):
    parsed = parse_time(raw, tz_hint=tz_hint, ref=ref)
    conf = SOURCE_CONFIDENCE[source] * KIND_FACTOR.get(parsed["kind"], 1.0)
    if parsed["tz_assumed"]:
        conf *= TZ_ASSUMED_FACTOR
    if parsed["value"] is None:
        conf = 0.0
    note = parsed["note"]
    if extra_note:
        note = (note + "; " if note else "") + extra_note
    return {"source": source, "role": role, "kind": parsed["kind"],
            "raw": str(raw), "value": parsed["value"],
            "confidence": round(conf, 4), "precision": parsed["precision"],
            "note": note}


def _build_candidates(rec: dict) -> list:
    cands = []
    clues = rec.get("time_clues") or {}

    rss = clues.get("rss") or {}
    if rss.get("published"):
        cands.append(_candidate("rss", "publish", rss["published"]))
    if rss.get("updated"):
        cands.append(_candidate("rss", "revision", rss["updated"]))

    meta = clues.get("page_meta") or {}
    if meta.get("published"):
        cands.append(_candidate("page_meta", "publish", meta["published"],
                                tz_hint=meta.get("tz")))
    if meta.get("modified"):
        cands.append(_candidate("page_meta", "revision", meta["modified"],
                                tz_hint=meta.get("tz")))

    body = clues.get("body") or {}
    ref = body.get("ref") or rec.get("fetched_at")
    if body.get("date"):
        cands.append(_candidate("body", "publish", body["date"],
                                tz_hint=body.get("tz"), ref=ref))
    if body.get("relative"):
        cands.append(_candidate("body", "publish", body["relative"],
                                tz_hint=body.get("tz"), ref=ref))

    # 旧记录字段兼容：裸字段按 legacy 线索读取；published_at 仅在不是本阶段
    # 产物（无 publish_time 块）时才当作输入，避免把自己的输出当线索循环放大。
    legacy_publish = [("ts", rec.get("ts")), ("date", rec.get("date"))]
    if "publish_time" not in rec:
        legacy_publish.append(("published_at", rec.get("published_at")))
    for field, raw in legacy_publish:
        if raw:
            cands.append(_candidate("legacy", "publish", raw,
                                    extra_note=f"legacy-field:{field}"))
    for field in ("updated", "modified"):
        if rec.get(field):
            cands.append(_candidate("legacy", "revision", rec[field],
                                    extra_note=f"legacy-field:{field}"))
    return cands


# ---- 采用与冲突 ----
def _decide(candidates: list) -> dict:
    pubs = [c for c in candidates if c["role"] == "publish" and c["value"]]
    revs = [c for c in candidates if c["role"] == "revision" and c["value"]]

    adopted = None
    if pubs:
        adopted = sorted(pubs, key=lambda c: (-c["confidence"],
                                              -SOURCE_PRIORITY[c["source"]],
                                              c["value"]))[0]
    revised_at = max((c["value"] for c in revs), default=None)

    block = {"published_at": None, "revised_at": revised_at,
             "source": None, "kind": None, "precision": None,
             "confidence": 0.0, "basis": "", "basis_code": "",
             "candidates": candidates, "revisions": []}

    if adopted is None:
        if revs:
            block["basis_code"] = "revision-only"
            block["basis"] = ("no publish candidate; revision time kept in "
                              "revised_at only (never used as publish time)")
        else:
            block["basis_code"] = "unrecoverable"
            block["basis"] = ("unrecoverable: no absolute or relative publish "
                              "time; processing date is NOT used as publish date")
        return block

    block.update(published_at=adopted["value"], source=adopted["source"],
                 kind=adopted["kind"], precision=adopted["precision"],
                 confidence=adopted["confidence"])

    conflicts = []
    for c in pubs:
        if c is adopted:
            continue
        delta = abs(_to_epoch(c["value"]) - _to_epoch(adopted["value"]))
        if delta > CONFLICT_TOLERANCE.total_seconds():
            conflicts.append((c, delta))
    agrees = [c for c in pubs if c is not adopted
              and all(c is not cf for cf, _ in conflicts)]

    parts = [f"adopted {adopted['source']}={adopted['value']} "
             f"(confidence {adopted['confidence']:.2f}, {adopted['kind']})"]
    if adopted["kind"] == "relative":
        block["basis_code"] = "relative-approximate"
        parts.append("approximate: derived from relative expression")
    elif conflicts:
        block["basis_code"] = "adopted-with-conflict"
    else:
        block["basis_code"] = "adopted"
    if conflicts:
        parts.append("conflict: " + "; ".join(
            f"{c['source']}={c['value']} (Δ{delta / 3600:.1f}h)"
            for c, delta in conflicts))
    if agrees:
        parts.append("agree: " + "; ".join(
            f"{c['source']}={c['value']}" for c in agrees))
    block["basis"] = " | ".join(parts)
    return block


def process(record):
    """流水线阶段：对单条记录做时间解释（纯函数，可重复执行）。"""
    rec = dict(record)
    prev = rec.get("publish_time") or {}
    block = _decide(_build_candidates(rec))
    block["revisions"] = list(prev.get("revisions") or [])
    rec["publish_time"] = block
    rec["published_at"] = block["published_at"]
    rec["revised_at"] = block["revised_at"]
    return rec


# ---- 合并：重复抓取 / 迟到补全 / 崩溃恢复后重跑 ----
_LEGACY_TIME_FIELDS = ("ts", "date", "updated", "modified", "fetched_at", "page")
# 身份类字段以首次入库为准，重复抓取不改写（保证键与引用稳定）
_PRESERVE_FROM_OLD = ("id", "article_key", "canonical_url")


def merge_records(old: dict, new: dict) -> dict:
    """把同一文章的两次观测合并为一份，时间解释保持一致。

    - time_clues 按来源取并集（新观测覆盖同来源线索；旧线索不丢，
      因此重复抓取缺少页面时不会把已还原的发布时间降级）；
    - 旧记录缺失的 legacy 时间字段补齐（旧记录字段兼容）；
    - 采用值发生变化时追加 revisions，来源可查。
    """
    merged = dict(new)
    clues = dict(old.get("time_clues") or {})
    clues.update(new.get("time_clues") or {})
    if clues:
        merged["time_clues"] = clues
    for field in _LEGACY_TIME_FIELDS:
        if field not in merged and field in old:
            merged[field] = old[field]
    for field in _PRESERVE_FROM_OLD:
        if old.get(field) is not None:
            merged[field] = old[field]

    prev_block = old.get("publish_time") or {}
    prev_published = prev_block.get("published_at")
    merged = process(merged)
    block = merged["publish_time"]
    block["revisions"] = list(prev_block.get("revisions") or [])
    if prev_block and prev_published != block["published_at"]:
        block["revisions"].append({
            "from": prev_published,
            "to": block["published_at"],
            "source": block["source"],
            "reason": f"merge: adopted {block['source']} ({block['basis_code']})",
        })
    return merged


# ---- 排序：稳定且可解释 ----
def sort_key(rec: dict):
    """"最新"列表排序键（升序排列即最新在前）。

    tier 0 有发布时间：按发布时间倒序（绝对优先于相对，同值按主键稳定）；
    tier 1 仅有修订时间：修订时间不参与"最新"，整层排在发布时间之后；
    tier 2 无法还原：垫底，按主键稳定（不拿处理当天充数）。
    """
    pt = rec.get("publish_time") or {}
    key = str(rec.get("article_key") or rec.get("id") or "")
    pub, rev = pt.get("published_at"), pt.get("revised_at")
    if not pt:  # 旧记录兼容：裸字段直接参与排序
        pub = rec.get("published_at") or rec.get("ts")
        if pub:
            parsed = parse_time(pub)["value"]
            if parsed:
                return (0, -_to_epoch(parsed), 2, key)
        pub = None
    if pub:
        kind_rank = 0 if pt.get("kind") == "absolute" else 1
        return (0, -_to_epoch(pub), kind_rank, key)
    if rev:
        return (1, -_to_epoch(rev), 0, key)
    return (2, 0, 0, key)


def sort_feed(records):
    """按 sort_key 排序的文章列表（最新在前，稳定）。"""
    return sorted(records, key=sort_key)
