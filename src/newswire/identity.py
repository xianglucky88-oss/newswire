"""文章身份规范化。

本阶段只回答一个问题：*这条记录看起来像哪篇文章*。它不判断重复，
也不改写发布/更新时间（那是 ``publish_time`` 的职责）。输出尽量做成
稳定、可复用的“身份证据”，后续 ``dedup`` 阶段直接沿用，避免在去重
逻辑里再实现一遍 URL/标题/正文的规范化。

身份由以下证据组成：

``canonical_url``
    去掉跟踪参数、片段、默认端口，并统一 http(s)、``www.``/``m.``/
    ``amp.`` 前缀后的 URL。只在确实能提高同一性判断时才做归并。

``title_norm`` / ``body_norm``
    Unicode NFKC、大小写折叠、HTML 实体解码、去标签、空白折叠后的
    标题与正文。

``content_digest``
    标题 + 正文（没有正文时用摘要/标题）的稳定摘要。**不包含来源和
    时间**，因此同一篇稿件被转载、或被同一媒体只修改了更新时间时，
    摘要不变；正文发生真实改版时摘要才会变化。

``aliases``
    所有可以用来找回同一篇文章的身份别名：原始 URL、规范 URL、
    guid/external id、``source:id`` 等。``store`` 会把这些别名落到
    别名表；``dedup`` 只消费这里的结果，不自己造别名。
"""
from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from typing import Any, Iterable
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

STAGE = "identity"

#: URL 查询参数中纯跟踪/分享用途的字段。保留其余参数并排序，避免
#: 参数顺序造成“两个 URL”。
_TRACKING_QUERY_NAMES = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "yclid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "ref",
    "referrer",
    "referer",
    "source",
    "src",
    "campaign",
    "spm",
    "share",
    "share_token",
    "_ga",
    "_gl",
    "_hsenc",
    "_hsmi",
    "mkt_tok",
    "trk",
    "trk_contact",
    "trk_module",
    "trk_sid",
    "cmpid",
    "ocid",
    "smid",
}
_TRACKING_QUERY_PREFIXES = ("utm_", "pk_", "mtm_", "hsa_")
_AMP_QUERY_NAMES = {"amp", "outputtype", "output"}

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_ZERO_WIDTH_RE = re.compile(r"[​-‏‪-‮﻿]")

_PREFERRED_TITLE_FIELDS = ("title", "headline", "name", "subject")
_FULL_BODY_FIELDS = ("body", "content", "article_body", "full_text", "text")
_SUMMARY_FIELDS = ("summary", "description", "abstract", "excerpt")
_PREFERRED_BODY_FIELDS = _FULL_BODY_FIELDS + _SUMMARY_FIELDS
_PREFERRED_URL_FIELDS = (
    "canonical_url",
    "canonical",
    "url",
    "link",
    "article_url",
    "permalink",
    "source_url",
)
_PREFERRED_SOURCE_FIELDS = (
    "source",
    "src",
    "publisher",
    "outlet",
    "media",
    "site",
    "site_name",
)
_PREFERRED_GUID_FIELDS = ("guid", "external_id", "article_id", "item_id")
#: 联合发布/联合署名来源（同一篇稿件由多家媒体同时发布）。
_CO_SOURCE_FIELDS = (
    "co_sources",
    "joint_sources",
    "co_publishers",
    "copublishers",
    "joint_publishers",
    "also_published_by",
    "syndicators",
    "sources",
)


def _source_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, dict):
        items = list(value.keys())
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    result: list[str] = []
    for item in items:
        names = re.split(r"[,;，；|/]+", _text(item)) if not isinstance(item, (list, tuple, dict)) else [_text(item)]
        for name in names:
            folded = normalized_source(name)
            if folded and folded not in result:
                result.append(folded)
    return result


def _first(record: dict[str, Any], names: Iterable[str], default: Any = "") -> Any:
    for name in names:
        value = record.get(name)
        if value is not None and value != "":
            return value
    return default


def _first_field(record: dict[str, Any], names: Iterable[str]) -> str:
    for name in names:
        if record.get(name) not in (None, ""):
            return name
    return ""


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return " ".join(_text(v) for v in value)
    return str(value)


def normalize_text(value: Any) -> str:
    """把标题/正文变成适合比较的形式（保留语义，不丢标点）。"""
    text = _text(value)
    text = html.unescape(text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _ZERO_WIDTH_RE.sub("", text)
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n+", "\n", text)
    return text.strip()


def _fold_text(value: Any) -> str:
    """在 ``normalize_text`` 基础上做大小写折叠，用于摘要和近似比较。"""
    return normalize_text(value).casefold()


def _digest(text: str, *, length: int = 32) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def looks_like_url(value: Any) -> bool:
    """``alpha:h1`` 这类 ``source:id`` 别名不是 URL；只有带 scheme 或像主机的才算。"""
    raw = _text(value).strip()
    if not raw:
        return False
    if raw.startswith("//"):
        return True
    if _SCHEME_RE.match(raw):
        return True
    if raw.startswith("/"):
        return False
    authority = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    host = authority.split("@")[-1].split(":")[0]
    return host == "localhost" or "." in host


def canonicalize_url(url: Any) -> str:
    """规范化文章 URL。

    * 去掉片段、跟踪参数、``amp`` 一类的输出参数；
    * 查询参数排序；
    * host 小写并去掉 ``www.`` / ``m.`` / ``amp.`` 前缀；
    * http 与 https 视为同一篇文章的两种表示；
    * 去掉非根路径末尾的 ``/``（以及常见的 ``/amp`` 后缀）。

    不是 URL 的输入（例如 ``alpha:h1``）原样返回，绝不猜测。
    """
    raw = _text(url).strip()
    if not raw:
        return ""
    if not looks_like_url(raw):
        return raw
    if raw.startswith("//"):
        raw = "https:" + raw

    # 没有 scheme 的 “example.com/a” 也当作 URL；相对路径保持原样。
    if not _SCHEME_RE.match(raw):
        if raw.startswith("/"):
            return raw.rstrip("/") or "/"
        raw = "https://" + raw

    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        # 非法端口等畸形输入：不做归并，返回原值。
        return _text(url).strip()
    scheme = parts.scheme.lower()
    if scheme in {"http", "https"}:
        scheme = "https"
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return raw
    for prefix in ("www.", "m.", "amp."):
        while host.startswith(prefix):
            host = host[len(prefix) :]
    if not host:
        return raw

    default_port = port in (80, 443)
    netloc = host if port is None or default_port else f"{host}:{port}"

    path = unquote(parts.path or "/")
    path = re.sub(r"/{2,}", "/", path)
    # 常见的 AMP 路径与尾斜杠表示。
    path = re.sub(r"/amp/?$", "", path, flags=re.IGNORECASE)
    if len(path) > 1:
        path = path.rstrip("/")
    if not path:
        path = "/"

    query_pairs: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        key_l = key.lower()
        if key_l in _AMP_QUERY_NAMES or key_l in _TRACKING_QUERY_NAMES:
            continue
        if any(key_l.startswith(prefix) for prefix in _TRACKING_QUERY_PREFIXES):
            continue
        query_pairs.append((key, value))
    query_pairs.sort(key=lambda kv: (kv[0].lower(), kv[1]))

    return urlunsplit((scheme, netloc, path, "&".join(f"{k}={v}" for k, v in query_pairs), ""))


def normalized_source(value: Any) -> str:
    source = _fold_text(value)
    # 支持 “Alpha 网” 这类展示名，但去除首尾标点，避免同一来源两个 key。
    source = re.sub(r"^[\s,;|·\-–—]+|[\s,;|·\-–—]+$", "", source)
    return source


def _source_from_url(url: str) -> str:
    if not url:
        return ""
    host = urlsplit(url).hostname or ""
    host = host.lower().rstrip(".")
    for prefix in ("www.", "m.", "amp."):
        while host.startswith(prefix):
            host = host[len(prefix) :]
    return host


def _raw_aliases(record: dict[str, Any]) -> list[str]:
    aliases: list[str] = []

    def add(value: Any) -> None:
        text = _text(value).strip()
        if text and text not in aliases:
            aliases.append(text)

    for field in ("url", "link", "article_url", "permalink", "source_url"):
        add(record.get(field))
    add(record.get("canonical_url"))
    add(record.get("canonical"))

    for field in ("aliases", "alias", "alt_urls", "alternate_urls", "also_at"):
        value = record.get(field)
        if isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)
        elif value:
            add(value)

    source = normalized_source(_first(record, _PREFERRED_SOURCE_FIELDS))

    def add_scoped(value: Any) -> None:
        """外部 id 只在同一来源内唯一：有来源时只写 ``source:id`` 作用域别名。

        裸 id 可能被不同媒体复用（``id=1``），作为全局别名会把两篇无关
        文章错误合并。只有它本身是 URL（全局唯一）或来源未知时才保留裸值。
        """
        text = _text(value).strip()
        if not text:
            return
        if text.lower().startswith(("http://", "https://", "//")):
            add(text)
            return
        if source:
            add(f"{source}:{text}")
        else:
            add(text)

    for field in ("guid", "external_id", "article_id", "item_id"):
        add_scoped(record.get(field))

    if record.get("id") not in (None, ""):
        add_scoped(record.get("id"))

    for field in ("source_id", "item_guid", "feed_guid"):
        add_scoped(record.get(field))
    return aliases


def identity_of(record: dict[str, Any]) -> dict[str, Any]:
    """计算并返回身份证据；不修改输入记录。"""
    url = canonicalize_url(_first(record, _PREFERRED_URL_FIELDS))
    title = normalize_text(_first(record, _PREFERRED_TITLE_FIELDS))
    body_field = _first_field(record, _PREFERRED_BODY_FIELDS)
    body = normalize_text(record.get(body_field)) if body_field else ""
    body_is_summary = body_field in _SUMMARY_FIELDS

    title_fold = title.casefold()
    body_fold = body.casefold()
    content_text = "\n".join(part for part in (title_fold, body_fold) if part)
    content_digest = _digest(content_text) if content_text else ""
    title_digest = _digest(title_fold) if title_fold else ""
    body_digest = _digest(body_fold) if body_fold else ""

    source = normalized_source(_first(record, _PREFERRED_SOURCE_FIELDS))
    if not source:
        source = _source_from_url(url)
    source_id = _text(_first(record, _PREFERRED_GUID_FIELDS))
    if not source_id and record.get("id") not in (None, ""):
        source_id = _text(record.get("id"))

    # 联合发布：主来源 + 共同署名来源，均作为本文档的来源证据。
    co_sources: list[str] = []
    for field in _CO_SOURCE_FIELDS:
        for name in _source_list(record.get(field)):
            if name != source and name not in co_sources:
                co_sources.append(name)
    all_sources = ([source] if source else []) + co_sources

    aliases = _raw_aliases(record)
    canonical_aliases: list[str] = []
    for alias in aliases:
        canonical = canonicalize_url(alias) if looks_like_url(alias) else ""
        if canonical and canonical not in canonical_aliases:
            canonical_aliases.append(canonical)
    if url and url not in canonical_aliases:
        canonical_aliases.insert(0, url)

    # 只保留真正有区分度的别名，并去重；source:id 对跨源找回很关键。
    alias_out: list[str] = []
    for alias in list(canonical_aliases) + aliases:
        if alias and alias not in alias_out:
            alias_out.append(alias)

    return {
        "canonical_url": url,
        "url": url,
        "title": title,
        "title_norm": title_fold,
        "body": body,
        "body_norm": body_fold,
        "body_field": body_field,
        "body_is_summary": body_is_summary,
        "title_digest": title_digest,
        "body_digest": body_digest,
        "content_digest": content_digest,
        "content_key": content_digest,
        "source": source,
        "sources": all_sources,
        "co_sources": co_sources,
        "source_id": source_id,
        "aliases": alias_out,
        "canonical_aliases": canonical_aliases,
    }


def process(record: dict[str, Any]) -> dict[str, Any]:
    """流水线阶段：附加规范身份，同时保留输入字段。"""
    rec = dict(record)
    ident = identity_of(rec)
    rec["identity"] = ident

    # 把最常用的证据提到顶层，方便 keyer/store 以及人工排查。
    if ident["canonical_url"]:
        rec["canonical_url"] = ident["canonical_url"]
    if ident["source"]:
        # 不覆盖调用方显式提供的 source；normalizer 可能只规整了 src。
        rec.setdefault("source", ident["source"])
    if ident["content_digest"]:
        rec["content_digest"] = ident["content_digest"]
    if ident["co_sources"]:
        rec["co_sources"] = list(ident["co_sources"])
    if ident["title_norm"]:
        rec["title_norm"] = ident["title_norm"]
    rec["identity_aliases"] = ident["aliases"]
    rec["_stage_identity"] = "ok"
    return rec


# 便于其他模块使用更直白的名字。
canonical_url = canonicalize_url
