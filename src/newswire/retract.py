"""单一职责：识别撤稿 / 更正标记并解析其目标。

本阶段不判断重复，也不删除文章；它把“这条记录携带了撤稿证据”翻译成
结构化字段：

``retraction``
    ``is_retraction``、``kind``（``retraction`` / ``withdrawal`` /
    ``correction``）、``target``（被撤/被更正文章的 URL 或别名）、
    ``reason``、``declared_at`` 与命中的证据字段。
``is_retraction`` / ``retraction_target``
    提升到顶层的便捷字段，供 ``dedup`` 直接使用。

识别依据（任一即可）：

* 显式字段：``retracted`` / ``withdrawn`` / ``is_retraction`` /
  ``retraction`` / ``status``；
* 目标字段：``retracts`` / ``retraction_of`` / ``withdraws`` /
  ``withdrawn_url`` / ``correction_of`` / ``retracted_url``；
* 标题或正文前缀：``[撤稿]``、``【撤稿】``、``Retracted:``、
  ``Withdrawal:``、``Correction:``、``撤稿声明`` 等。

撤稿与更正是两类不同证据：更正会新增内容版本，撤稿会改写文章状态；两者
都必须保留在历史里，不能通过删除记录来表达。``target`` 缺失时，只有记录
自身被标记状态（``status=retracted`` 等）才回退到自身 URL——标题里的撤稿
字样不足以断言“这篇就是被撤的那篇”。
"""
from __future__ import annotations

import re
from typing import Any

STAGE = "retract"

_TARGET_FIELDS = (
    "retracts",
    "retraction_of",
    "retracted_url",
    "withdraws",
    "withdrawn_url",
    "withdrawal_of",
    "correction_of",
    "corrects",
)

_RETRACT_VALUES = {
    "retracted",
    "retract",
    "withdrawn",
    "withdraw",
    "removed",
    "deleted",
    "撤稿",
    "撤回",
    "已撤稿",
    "已删除",
}
_CORRECTION_VALUES = {"corrected", "correction", "corrected_and_republished", "更正", "已更正"}

_TITLE_MARKERS = (
    (re.compile(r"^\s*[\[【(（]\s*(撤稿|撤回|删除)\s*[\]】)）]", re.IGNORECASE), "retraction"),
    (re.compile(r"^\s*(撤稿|撤回)(声明|通知|公告)", re.IGNORECASE), "retraction"),
    (re.compile(r"^\s*retract(ed|ion)\s*[:：\-]", re.IGNORECASE), "retraction"),
    (re.compile(r"^\s*withdraw(al|n)\s*[:：\-]", re.IGNORECASE), "withdrawal"),
    (re.compile(r"^\s*[\[【(（]\s*更正\s*[\]】)）]", re.IGNORECASE), "correction"),
    (re.compile(r"^\s*(更正|勘误)\s*[:：]", re.IGNORECASE), "correction"),
    (re.compile(r"^\s*correction\s*[:：\-]", re.IGNORECASE), "correction"),
)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return " ".join(_text(v) for v in value)
    if isinstance(value, dict):
        return " ".join(f"{k}={_text(v)}" for k, v in value.items())
    return str(value)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = _text(value).strip().casefold()
    return text in {"1", "true", "yes", "y", "on", "retracted", "withdrawn", "撤稿", "撤回"}


def _present(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def detect(record: dict[str, Any]) -> dict[str, Any]:
    """返回撤稿证据；没有证据时 ``is_retraction`` 为 False。"""
    evidence: list[str] = []
    kind = ""
    target = ""
    reason = ""
    declared_at = ""
    self_marked = False

    retraction = record.get("retraction")
    if isinstance(retraction, dict):
        if retraction.get("is_retraction") or retraction.get("retracted"):
            kind = _text(retraction.get("kind") or retraction.get("type") or "retraction").casefold()
            evidence.append("retraction")
        for key in ("target", "url", "article_url", "retracts", "retraction_of"):
            if retraction.get(key):
                target = _text(retraction.get(key)).strip()
                evidence.append(f"retraction.{key}")
                break
        if retraction.get("reason"):
            reason = _text(retraction.get("reason"))
        if retraction.get("declared_at") or retraction.get("date"):
            declared_at = _text(retraction.get("declared_at") or retraction.get("date"))
    elif _present(retraction):
        evidence.append("retraction")
        if isinstance(retraction, str) and retraction.strip().lower().startswith(("http://", "https://", "//")):
            target = retraction.strip()
        elif _truthy(retraction):
            kind = "retraction"

    for field in ("retracted", "withdrawn", "is_retraction", "is_correction"):
        if field in record and _truthy(record.get(field)):
            evidence.append(field)
            if field == "is_correction":
                kind = "correction"
            elif field == "withdrawn":
                kind = "withdrawal"
            else:
                kind = kind or "retraction"
            self_marked = True

    status = _text(record.get("status")).strip().casefold()
    if status:
        if status in _RETRACT_VALUES or any(token in status for token in ("撤稿", "撤回")):
            evidence.append("status")
            kind = "withdrawal" if ("withdraw" in status or status == "撤回") else "retraction"
            self_marked = True
        elif status in _CORRECTION_VALUES or "更正" in status:
            evidence.append("status")
            kind = kind or "correction"
            self_marked = True

    for field in _TARGET_FIELDS:
        value = record.get(field)
        if _present(value):
            target = _text(value).strip()
            evidence.append(field)
            if field in {"correction_of", "corrects"} and not kind:
                kind = "correction"
            elif not kind:
                kind = "retraction"
            break

    title = _text(record.get("title") or record.get("headline") or record.get("name"))
    body = _text(
        record.get("summary") or record.get("description") or record.get("body") or record.get("content")
    )
    marker_hit = False
    for text_value, source in ((title, "title"), (body[:200], "body")):
        for pattern, marker_kind in _TITLE_MARKERS:
            if pattern.search(text_value):
                evidence.append(f"{source}_marker")
                kind = kind or marker_kind
                marker_hit = True
                break
        if marker_hit:
            break

    for field in ("retraction_reason", "withdrawal_reason", "reason"):
        if not reason and record.get(field):
            reason = _text(record.get(field))
            evidence.append(field)
    for field in ("retracted_at", "withdrawn_at", "declared_at"):
        if not declared_at and record.get(field):
            declared_at = _text(record.get(field))
            evidence.append(field)

    is_retraction = bool(evidence)
    if is_retraction and not kind:
        kind = "retraction"

    # target 缺失时，只有“自身被标记状态”才回退到自身 URL。标题带撤稿字样
    # 但没有目标的记录，是一个需要单独承载的撤稿声明。
    if is_retraction and not target and (self_marked or not marker_hit):
        for field in ("canonical_url", "url", "link"):
            if record.get(field):
                target = _text(record.get(field)).strip()
                evidence.append(f"self:{field}")
                break

    return {
        "is_retraction": is_retraction,
        "kind": kind,
        "target": target,
        "reason": reason,
        "declared_at": declared_at,
        "evidence": evidence,
        "self_marked": self_marked,
    }


def process(record: dict[str, Any]) -> dict[str, Any]:
    rec = dict(record)
    info = detect(rec)
    rec["retraction"] = info
    rec["is_retraction"] = info["is_retraction"]
    if info["is_retraction"]:
        rec["retraction_kind"] = info["kind"]
        if info["target"]:
            rec["retraction_target"] = info["target"]
    rec["_stage_retract"] = "ok" if info["is_retraction"] else "none"
    return rec
