"""单一职责：判定记录级重复或修订，并产出文章聚合。

本模块消费上游阶段的证据，只做判定、不持久化：

``identity``
    规范 URL、标题/正文摘要、身份别名、规范来源与联合发布来源。
``publish_time``
    ``published_at``（媒体声明时间）、``updated_at``（媒体更新时间）、
    发布区间与 ``first_seen``（抓取时间）。
``retract``
    撤稿/更正声明及其目标。

输出一份“文章文档”，交给 ``keyer`` / ``store``：

``key``
    稳定文章键。由来源集合中的规范 URL（或来源+外部 id / 内容摘要）派生；
    抓取时间、更新时间、批次、输入顺序都不参与，因此同一媒体反复修改
    updated 不会生成新文章。
``versions``
    不可变内容版本链。每个版本保存内容摘要、标题/正文、来源、发布时间
    与 URL。内容完全一致的重复抓取/转载**不追加版本**，只有真实正文改版
    才追加，修订证据不会因为时间戳变化而膨胀。
``sources``
    参与过这篇文章的来源集合，以及每个来源的首次/最近出现时间、URL 和
    角色（origin / syndication / revision / retraction_notice ...），外加
    去重后的观察记录（``observations``）。
``events``
    关系事件：``syndication``（转载）、``revision``（同文改版）、
    ``retraction`` / ``withdrawal`` / ``correction``（撤稿/更正）、
    ``republication``（撤稿后重发）、``observed``（仅时间变化的重复观察）。
``status``
    ``active`` / ``retracted`` / ``withdrawn`` / ``corrected``。

判定顺序：

1. **撤稿/更正声明**按目标 URL/别名找回被撤文章，把状态与事件写回目标，
   不新建文章；找不到目标时，内容一致则合并，否则作为承载撤稿状态的
   新文章（历史不删除）。
2. **身份别名 / 规范 URL 相同** → 同一篇文章。内容不同追加版本
   （``revision``）；内容相同或只有标题可比的重复观察不追加版本。
3. **内容摘要相同** → 同一篇稿件：
   * 文章处于撤稿状态且记录不早于撤稿 → ``republication``（撤稿后重发）；
   * 发布时间在任一已有版本的窗口内（默认 ±24h）→ ``syndication``；
   * 双方发布时间都可靠且都超出窗口 → **独立新文**。
4. **内容近似**（正文一致、标题相同且正文相似、或一方是 RSS 摘要）→
   窗口内为 ``revision``，窗口外为新文。
5. 仅有相同标题、双方都无正文 → 不足以断言同一篇，按新文处理。
6. 其余 → 新文章。

取舍规则：内容近似但发布时间不同
--------------------------------
* 内容完全一致：与**任一**已有版本的发布时间相距在窗口内 → 同一篇
  （转载）；全部超出窗口 → 视作真正的新文（多年后重发同一通稿），但
  仍在新文上留下指向旧文的 ``republication`` 证据。
* 内容近似但不完全一致：只在窗口内合并；窗口外一律新文，避免把同主题
  的系列报道/持续更新吸成一篇文章。
* 任一方缺少可靠的媒体发布时间（``published_kind != "declared"``，
  例如只有抓取时间 ``ts``）或缺少正文时不做“超出窗口即新文”的判断；
  宁可合并为转载，也不因抓取时间差异凭空新增文章。
* 撤稿目标优先于时间规则：声明指向的旧文一定合并，即使时间相差很远。
* 历史不倒退：早于撤稿声明的记录回放只记为 ``stale_observation``，不会
  把文章从 ``retracted`` 复活。

模块不修改输入记录；返回 ``{"article", "match", "key"}``，``pipeline``
再把 ``article`` 交给 ``keyer`` / ``store``。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterable

from . import identity as _identity
from . import publish_time as _publish_time

STAGE = "dedup"

WINDOW_HOURS = _publish_time.DEFAULT_WINDOW_HOURS
NEAR_TITLE_RATIO = 0.92
NEAR_BODY_RATIO = 0.85
NEAR_COMBINED_RATIO = 0.90


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _hash(prefix: str, payload: Any, length: int = 24) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return prefix + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:length]


def _fold(value: Any) -> str:
    return _identity._fold_text(value)  # noqa: SLF001 — 与 identity 共用同一规范化语义


def _source_of(ident: dict[str, Any]) -> str:
    return _text(ident.get("source")).strip().casefold()


def _sources_of(ident: dict[str, Any]) -> list[str]:
    """联合发布的全部来源；``source`` 始终排在第一位。"""
    primary = _source_of(ident)
    result = [primary] if primary else []
    for name in ident.get("sources") or []:
        folded = _text(name).strip().casefold()
        if folded and folded not in result:
            result.append(folded)
    for name in ident.get("co_sources") or []:
        folded = _text(name).strip().casefold()
        if folded and folded not in result:
            result.append(folded)
    return result


def _aliases_for(ident: dict[str, Any]) -> list[str]:
    aliases = [str(a) for a in (ident.get("aliases") or []) if a]
    url = ident.get("canonical_url") or ident.get("url") or ""
    if url and url not in aliases:
        aliases.insert(0, url)
    return aliases


def _is_reliable(time_ev: dict[str, Any]) -> bool:
    return time_ev.get("published_kind") == "declared" and time_ev.get("published_epoch") is not None


def _epoch(time_ev: dict[str, Any]) -> int | None:
    value = time_ev.get("published_epoch")
    return int(value) if value is not None else None


def _version_time(version: dict[str, Any]) -> dict[str, Any]:
    return {
        "published_at": version.get("published_at", ""),
        "published_epoch": version.get("published_epoch"),
        "published_kind": version.get("published_kind", "declared"),
        "window_hours": version.get("window_hours", WINDOW_HOURS),
    }


def _within_window(time_ev: dict[str, Any], other: dict[str, Any]) -> bool:
    """发布时间是否落在窗口内；缺可靠时间时返回 True（不因时间拆文）。"""
    other_epoch = _epoch(other)
    epoch = _epoch(time_ev)
    if epoch is None or other_epoch is None:
        return True
    if not _is_reliable(time_ev) or not _is_reliable(other):
        return True
    window = max(
        int(time_ev.get("window_hours") or WINDOW_HOURS),
        int(other.get("window_hours") or WINDOW_HOURS),
    )
    return abs(epoch - other_epoch) <= window * 3600


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _content_relation(incoming: dict[str, Any], other: dict[str, Any]) -> str:
    """``same``/``near``/``title_only``/``different``。

    ``title_only``：双方都没有正文，只能凭标题匹配；不同来源时不足以认定
    是同一篇稿件（固定栏目“每日早报”会复用同一标题），交给调用方决定是否
    需要其他身份证据。
    """
    digest_in = incoming.get("content_digest") or ""
    digest_other = other.get("content_digest") or ""
    body_in = incoming.get("body_norm") or ""
    body_other = other.get("body_norm") or ""
    if digest_in and digest_in == digest_other:
        return "same" if (body_in or body_other) else "title_only"

    title_in, title_other = incoming.get("title_norm") or "", other.get("title_norm") or ""
    tdigest_in, tdigest_other = incoming.get("title_digest") or "", other.get("title_digest") or ""
    bdigest_in, bdigest_other = incoming.get("body_digest") or "", other.get("body_digest") or ""

    # 正文完全一致（标题微调） → 同文改版。
    if body_in and body_in == body_other:
        return "near"
    if bdigest_in and bdigest_in == bdigest_other and not (body_in and body_other):
        return "near"
    # 标题完全一致，正文高度相似 → 改版。
    if (title_in and title_in == title_other) or (tdigest_in and tdigest_in == tdigest_other):
        if not body_in and not body_other:
            # 两侧都缺正文时，仅凭相同标题不足以合并：像“每日早报”这类
            # 固定栏目每天用同一标题。
            return "title_only"
        if not body_in or not body_other:
            return "near"
        if incoming.get("body_is_summary") or other.get("body_is_summary"):
            # 摘要与全文天然不相似；同标题 + 摘要证据按近似处理，由时间窗口
            # 决定是转载还是新文。
            return "near"
        if _similarity(body_in, body_other) >= NEAR_BODY_RATIO:
            return "near"
        return "different"
    # 标题和正文都只是近似 → 需要更高门槛，避免系列报道被误合并。
    if title_in and title_other and body_in and body_other:
        if (
            _similarity(title_in, title_other) >= NEAR_TITLE_RATIO
            and _similarity(body_in, body_other) >= NEAR_COMBINED_RATIO
        ):
            return "near"
    return "different"


def _version_ident(version: dict[str, Any]) -> dict[str, Any]:
    return {
        "content_digest": version.get("content_digest", ""),
        "title_digest": version.get("title_digest", ""),
        "body_digest": version.get("body_digest", ""),
        "title_norm": version.get("title_norm") or _fold(version.get("title", "")),
        "body_norm": version.get("body_norm", ""),
        "body_is_summary": version.get("body_is_summary", False),
    }


def _has_full_body(ident: dict[str, Any]) -> bool:
    """正文是否来自完整正文（而不是 RSS 摘要/描述）。"""
    return bool(ident.get("body_norm")) and not ident.get("body_is_summary")


def _is_impoverished(article: "Article", ident: dict[str, Any]) -> bool:
    """这条记录的内容证据是否残缺（例如只来自 RSS 摘要）。"""
    if _has_full_body(ident):
        return False
    if not ident.get("body_norm") and not ident.get("title_digest"):
        return False
    return any(_has_full_body_version(v) for v in article.versions)


def _has_full_body_version(version: dict[str, Any]) -> bool:
    return bool(version.get("body_norm")) and not version.get("body_is_summary")


def _incomplete_content(article: "Article", ident: dict[str, Any]) -> bool:
    """记录与文章至少一方缺完整正文：没有足够证据宣布“这是一篇新文”。"""
    return not _has_full_body(ident) or any(not _has_full_body_version(v) for v in article.versions)


def _version_of(ident: dict[str, Any], time_ev: dict[str, Any], source: str, url: str) -> dict[str, Any]:
    return {
        "content_digest": ident.get("content_digest", ""),
        "title_digest": ident.get("title_digest", ""),
        "body_digest": ident.get("body_digest", ""),
        "title": ident.get("title", ""),
        "title_norm": ident.get("title_norm", ""),
        "body_norm": ident.get("body_norm", ""),
        "body_is_summary": ident.get("body_is_summary", False),
        "published_at": time_ev.get("published_at", ""),
        "published_epoch": time_ev.get("published_epoch"),
        "published_kind": time_ev.get("published_kind", ""),
        "window_hours": time_ev.get("window_hours", WINDOW_HOURS),
        "updated_at": time_ev.get("updated_at", ""),
        "source": source,
        "url": url,
    }


def _event_payload(
    ident: dict[str, Any], time_ev: dict[str, Any], source: str, url: str, **extra: Any
) -> dict[str, Any]:
    payload = {
        "source": source,
        "url": url,
        "content_digest": ident.get("content_digest", ""),
        "published_at": time_ev.get("published_at", ""),
        "updated_at": time_ev.get("updated_at", ""),
        "observed_at": time_ev.get("first_seen", "") or time_ev.get("published_at", ""),
        "title": ident.get("title", ""),
    }
    payload.update(extra)
    return payload


@dataclass
class Article:
    """一篇聚合后的文章。"""

    key: str = ""
    canonical_url: str = ""
    title: str = ""
    source_id: str = ""
    status: str = "active"
    first_published_at: str = ""
    last_updated_at: str = ""
    content_digest: str = ""
    versions: list[dict[str, Any]] = field(default_factory=list)
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    identity_aliases: list[str] = field(default_factory=list)

    # --- 结构维护 -----------------------------------------------------
    def add_alias(self, alias: str) -> None:
        if not alias:
            return
        if alias not in self.aliases:
            self.aliases.append(alias)
        if alias not in self.identity_aliases:
            self.identity_aliases.append(alias)

    def has_version(self, content_digest: str) -> bool:
        if not content_digest:
            return False
        return any(v.get("content_digest") == content_digest for v in self.versions)

    def add_version(self, version: dict[str, Any]) -> bool:
        digest = version.get("content_digest", "")
        if self.has_version(digest):
            return False
        self.versions.append(version)
        if digest:
            self.content_digest = digest
        published = version.get("published_at") or ""
        if published and (not self.first_published_at or published < self.first_published_at):
            self.first_published_at = published
        updated = version.get("updated_at") or published
        if updated and updated > self.last_updated_at:
            self.last_updated_at = updated
        if version.get("title") and not self.title:
            self.title = version["title"]
        if not self.canonical_url and version.get("url"):
            self.canonical_url = version["url"]
        return True

    def add_source(self, source: str, *, alias: str = "", published_at: str = "", role: str = "", updated_at: str = "") -> None:
        if not source:
            return
        entry = self.sources.setdefault(
            source,
            {
                "source": source,
                "first_seen": "",
                "last_seen": "",
                "urls": [],
                "roles": [],
                "updated_at": "",
                "observations": [],
            },
        )
        entry.setdefault("observations", [])
        if alias and alias not in entry["urls"]:
            entry["urls"].append(alias)
        if role and role not in entry["roles"]:
            entry["roles"].append(role)
        if published_at:
            if not entry.get("first_seen") or published_at < entry["first_seen"]:
                entry["first_seen"] = published_at
            if not entry.get("last_seen") or published_at > entry["last_seen"]:
                entry["last_seen"] = published_at
        if updated_at and updated_at > (entry.get("updated_at") or ""):
            entry["updated_at"] = updated_at

    def observe(self, source: str, url: str, ident: dict[str, Any], time_ev: dict[str, Any]) -> bool:
        """登记一次观察；完全相同（来源+URL+内容+时间）的观察只记一次。

        返回 True 表示这是新的观察证据。旧批次回放因此不会让观察记录无限
        增长，而媒体真正修改 updated 时会留下新证据。
        """
        if not source:
            return False
        self.add_source(source, alias=url)
        signature = {
            "url": url,
            "content_digest": ident.get("content_digest", ""),
            "published_at": time_ev.get("published_at", ""),
            "updated_at": time_ev.get("updated_at", ""),
        }
        entry = self.sources[source]
        observations = entry.setdefault("observations", [])
        if signature in observations:
            return False
        observations.append(signature)
        return True

    def add_event(self, type_: str, payload: dict[str, Any]) -> bool:
        natural = {
            "type": type_,
            "source": payload.get("source", ""),
            "content_digest": payload.get("content_digest", ""),
            "published_at": payload.get("published_at", ""),
            # 媒体更新时间变化会产生独立证据；完全相同的重放不追加事件，
            # 保证同一批次反复运行收敛到同一事件集合。
            "updated_at": payload.get("updated_at", ""),
            "target": payload.get("target", ""),
            "kind": payload.get("kind", ""),
        }
        event_key = payload.get("event_key") or _hash("ev-", natural)
        for event in self.events:
            if event.get("event_key") == event_key:
                # 幂等：同一事件重复出现，只刷新时间证据，不追加。
                if payload.get("updated_at") and event.get("updated_at") != payload.get("updated_at"):
                    event["updated_at"] = payload.get("updated_at")
                return False
        event = dict(payload)
        event["type"] = type_
        event["event_key"] = event_key
        self.events.append(event)
        return True

    def versions_by_time(self) -> list[dict[str, Any]]:
        return sorted(
            self.versions,
            key=lambda v: (v.get("published_at") or "", v.get("content_digest") or ""),
        )

    # --- 序列化 -------------------------------------------------------
    def to_doc(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "source_id": self.source_id,
            "status": self.status,
            "first_published_at": self.first_published_at,
            "last_updated_at": self.last_updated_at,
            "content_digest": self.content_digest,
            "versions": list(self.versions),
            "sources": dict(self.sources),
            "events": list(self.events),
            "aliases": list(self.aliases),
            "identity_aliases": list(self.identity_aliases),
            "version_count": len(self.versions),
            "source_count": len(self.sources),
            "event_count": len(self.events),
        }

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> "Article":
        return cls(
            key=_text(doc.get("key")),
            canonical_url=_text(doc.get("canonical_url")),
            title=_text(doc.get("title")),
            source_id=_text(doc.get("source_id")),
            status=_text(doc.get("status")) or "active",
            first_published_at=_text(doc.get("first_published_at")),
            last_updated_at=_text(doc.get("last_updated_at")),
            content_digest=_text(doc.get("content_digest")),
            versions=list(doc.get("versions") or []),
            sources=dict(doc.get("sources") or {}),
            events=list(doc.get("events") or []),
            aliases=list(doc.get("aliases") or []),
            identity_aliases=list(doc.get("identity_aliases") or []),
        )


def _article_for(ident: dict[str, Any], time_ev: dict[str, Any]) -> Article:
    from .keyer import stable_article_key

    url = ident.get("canonical_url") or ident.get("url") or ""
    source = _source_of(ident)
    article = Article(
        key=stable_article_key(ident),
        canonical_url=url,
        title=ident.get("title", ""),
        source_id=_text(ident.get("source_id")),
    )
    for alias in _aliases_for(ident):
        article.add_alias(alias)
    article.add_version(_version_of(ident, time_ev, source, url))
    for name in _sources_of(ident):
        article.add_source(
            name,
            alias=url,
            published_at=time_ev.get("published_at", ""),
            role="origin" if name == source else "co_publisher",
        )
    return article


class _Index:
    """运行期索引。可由 store 中的文章重建，因此重入/回放结果可复现。"""

    def __init__(self, articles: Iterable[Article] = ()):
        self.articles: dict[str, Article] = {}
        self.by_alias: dict[str, str] = {}
        self.by_content: dict[str, str] = {}
        self.by_title: dict[str, list[str]] = {}
        for article in articles:
            self.add(article)

    def add(self, article: Article) -> None:
        self.articles[article.key] = article
        for alias in list(article.identity_aliases) + list(article.aliases):
            if alias:
                self.by_alias.setdefault(alias, article.key)
        if article.canonical_url:
            self.by_alias.setdefault(article.canonical_url, article.key)
        for version in article.versions:
            digest = version.get("content_digest")
            if digest:
                self.by_content.setdefault(digest, article.key)
            title_digest = version.get("title_digest")
            if title_digest:
                bucket = self.by_title.setdefault(title_digest, [])
                if article.key not in bucket:
                    bucket.append(article.key)

    def lookup_alias(self, alias: str) -> Article | None:
        key = self.by_alias.get(alias)
        return self.articles.get(key) if key else None

    def lookup_aliases(self, aliases: Iterable[str]) -> tuple[Article | None, str]:
        for alias in aliases:
            article = self.lookup_alias(alias)
            if article is not None:
                return article, alias
        return None, ""

    def _pick_title_candidate(
        self, ident: dict[str, Any], time_ev: dict[str, Any], keys: Iterable[str]
    ) -> Article | None:
        """同标题有多篇候选时，选内容最接近的；证据不足则不猜。"""
        candidates = [self.articles[k] for k in keys if k in self.articles]
        if not candidates:
            return None

        def score(article: Article) -> tuple[int, int, float]:
            relation_best = 0
            distance_best = float("inf")
            for version in article.versions:
                rel = _content_relation(ident, _version_ident(version))
                if rel == "same":
                    relation_best = 2
                elif rel in {"near", "title_only"} and relation_best < 2:
                    relation_best = 1
                other = _version_time(version)
                if _is_reliable(time_ev) and _is_reliable(other):
                    distance_best = min(distance_best, abs(_epoch(time_ev) - _epoch(other)))
            # 先比内容接近度，再比时间距离；缺失时间的候选不占优。
            distance = min(distance_best, 10**12) if distance_best != float("inf") else 10**12
            return (relation_best, 1 if distance_best != float("inf") else 0, -distance)

        best = max(candidates, key=score)
        relation_best = score(best)[0]
        if relation_best >= 2:
            return best
        if not _has_full_body(ident):
            # 新记录本身内容残缺（只有标题/摘要）：谨慎合并到最接近的一篇。
            return best
        if any(
            _content_relation(ident, _version_ident(version)) == "near"
            for version in best.versions
        ):
            return best
        # 标题相同但正文完全不同：交给“新文”判断，避免把同题系列报道误并。
        return None

    def lookup_content(self, ident: dict[str, Any], time_ev: dict[str, Any] | None = None) -> tuple[Article | None, str]:
        digest = ident.get("content_digest") or ""
        if digest:
            key = self.by_content.get(digest)
            if key:
                return self.articles.get(key), "content_digest"
        title_digest = ident.get("title_digest") or ""
        if title_digest:
            keys = self.by_title.get(title_digest) or []
            if keys:
                article = self._pick_title_candidate(ident, time_ev or {}, keys)
                if article is not None:
                    return article, "title_digest"
        return None, ""


def _find_by_retraction_target(
    index: _Index, retraction: dict[str, Any], ident: dict[str, Any]
) -> tuple[Article | None, str]:
    targets: list[str] = []
    target = _text(retraction.get("target")).strip()
    if target:
        canonical = _identity.canonicalize_url(target)
        for candidate in (canonical, target):
            if candidate and candidate not in targets:
                targets.append(candidate)
    for alias in _aliases_for(ident):
        if alias and alias not in targets:
            targets.append(alias)
    return index.lookup_aliases(targets)


def _record_sources(
    article: "Article", ident: dict[str, Any], url: str, time_ev: dict[str, Any], role: str
) -> None:
    """把这条记录的全部来源（含联合发布）登记到文章来源集合。"""
    for name in _sources_of(ident):
        article.add_source(
            name,
            alias=url,
            published_at=time_ev.get("published_at", ""),
            updated_at=time_ev.get("updated_at", ""),
            role=role,
        )


def _observe_all(article: "Article", ident: dict[str, Any], url: str, time_ev: dict[str, Any]) -> bool:
    """为记录的全部来源登记观察；任一来源是新观察即返回 True。"""
    is_new = False
    for name in _sources_of(ident):
        if article.observe(name, url, ident, time_ev):
            is_new = True
    return is_new


def _apply_retraction(
    article: Article, ident: dict[str, Any], time_ev: dict[str, Any], retraction: dict[str, Any]
) -> None:
    kind = _text(retraction.get("kind")) or "retraction"
    source = _source_of(ident)
    url = ident.get("canonical_url") or ident.get("url") or ""
    declared_at = _text(retraction.get("declared_at")) or time_ev.get("published_at", "")
    # 历史不允许被“倒退”：后到的、时间更早的声明不能把已撤稿改成 active，
    # 也不能把已更正改回 active。
    if kind == "correction":
        if article.status == "active":
            article.status = "corrected"
    elif kind == "withdrawal":
        if article.status != "retracted":
            article.status = "withdrawn"
    else:
        article.status = "retracted"
    article.add_event(
        kind if kind in {"retraction", "withdrawal", "correction"} else "retraction",
        _event_payload(
            ident,
            time_ev,
            source,
            url,
            target=_text(retraction.get("target")),
            kind=kind,
            reason=_text(retraction.get("reason")),
            declared_at=declared_at,
            evidence=list(retraction.get("evidence") or []),
        ),
    )
    for alias in _aliases_for(ident):
        article.add_alias(alias)


def _retraction_epoch(article: "Article") -> int | None:
    """文章最近一次撤稿/撤下声明的时间（epoch 秒）；没有则 None。"""
    latest: int | None = None
    for event in article.events:
        if event.get("type") not in {"retraction", "withdrawal"}:
            continue
        stamp = event.get("declared_at") or event.get("published_at") or ""
        parsed = _publish_time.parse_time(stamp)
        if parsed is not None:
            epoch = int(parsed.timestamp())
            if latest is None or epoch > latest:
                latest = epoch
    return latest


def _is_stale_observation(article: "Article", time_ev: dict[str, Any]) -> bool:
    """这条记录的所有时间证据是否都早于文章的撤稿声明。

    旧批次回放必须幂等：重放撤稿前的记录不能把文章从 retracted 复活，
    否则历史状态会随回放顺序漂移。判定用记录上最晚的已知时间（发布时间
    优先，其次抓取时间）；完全没有任何时间证据时按回放处理，宁可少记一次
    重发，也不让撤稿状态被无证据的记录抹掉。
    """
    retracted = _retraction_epoch(article)
    if retracted is None:
        return False
    stamps = [
        _epoch(time_ev),
        int(_publish_time.parse_time(time_ev.get("first_seen", "")).timestamp())
        if _publish_time.parse_time(time_ev.get("first_seen", ""))
        else None,
    ]
    known = [stamp for stamp in stamps if stamp is not None]
    if not known:
        return True
    return max(known) <= retracted


def _decide(
    article: Article, ident: dict[str, Any], time_ev: dict[str, Any], *, via_alias: bool = False
) -> tuple[str, str]:
    """返回 (关系, 原因)。关系 ∈ same_url / same_content / near_content / distinct。

    ``via_alias`` 表示记录已通过规范 URL / ``source:id`` 等身份别名找回同一
    篇文章。此时身份证据优先：它一定要落在这篇文章上，时间窗口只用来区分
    “转载 / 改版 / 重发”，不能用来拆成新文章。
    """
    incoming_url = ident.get("canonical_url") or ident.get("url") or ""
    same_url = bool(incoming_url and article.canonical_url and incoming_url == article.canonical_url)
    # 同一 URL 曾作为某版本的地址出现（文章主 URL 可能来自更早的来源）。
    same_url = same_url or any(
        incoming_url and version.get("url") == incoming_url for version in article.versions
    )
    if same_url:
        if article.status in {"retracted", "withdrawn"}:
            # 同一 URL 上重复抓取：撤稿前的时间戳是回放，撤稿后才是重发。
            if _is_stale_observation(article, time_ev):
                return "same_url", "stale_observation"
            return "same_url", "republication"
        for version in article.versions:
            rel = _content_relation(ident, _version_ident(version))
            if rel in {"same", "title_only"}:
                return "same_url", "canonical_url_same_content"
        if _is_impoverished(article, ident):
            # 摘要副本不是内容改版：同一 URL 上不应因此新增版本。
            return "same_url", "canonical_url_observed"
        if not article.versions:
            return "same_url", "canonical_url"
        return "same_url", "canonical_url_revision"

    best = "different"
    for version in article.versions:
        rel = _content_relation(ident, _version_ident(version))
        if rel == "same":
            best = "same"
            break
        if rel == "near":
            best = "near"

    # 发布时间窗口：与任一版本相近即可保持为同一篇文章（转载链）。
    in_window = any(_within_window(time_ev, _version_time(v)) for v in article.versions)

    if via_alias:
        # 身份别名命中：无论内容差异多大都必须归到这篇文章（上游身份是
        # 权威的），时间与内容只用于决定事件类型。
        if best == "different":
            return "near_content", "alias_revision"
        if best == "title_only":
            # 同一 URL / 同一 source:id，但没有正文可比：这是重复观察。
            return "same_content", "alias_title_only"
        if best == "same":
            if article.status in {"retracted", "withdrawn"} and not _is_stale_observation(article, time_ev):
                return "same_content", "republication"
            return "same_content", "syndication"
        return "near_content", "revision"

    if best == "different":
        return "distinct", "content"

    if best == "title_only":
        # 不同 URL、都缺正文，仅标题相同：不足以断言同一篇。
        return "distinct", "title_only"

    if best == "same":
        if article.status in {"retracted", "withdrawn"} and not _is_stale_observation(article, time_ev):
            return "same_content", "republication"
        if not in_window:
            # 证据残缺（缺正文）时不做“超出窗口即新文”的判断：宁可合并，
            # 也不因为只有标题/摘要的副本凭空新增文章。
            if _incomplete_content(article, ident):
                return "same_content", "syndication"
            return "distinct", "outside_window"
        return "same_content", "syndication"

    if in_window or _incomplete_content(article, ident):
        return "near_content", "revision"
    return "distinct", "near_outside_window"


def process(
    record: dict[str, Any],
    ctx: dict[str, Any] | None = None,
    *,
    index: "_Index | None" = None,
    articles: Iterable[dict[str, Any] | Article] = (),
) -> dict[str, Any]:
    """判定记录与已有文章的关系。

    ``ctx`` 由 ``pipeline`` 传入并在一次运行内共享；首次使用时从
    ``ctx["articles"]`` 或 ``ctx["con"]``（store 连接）载入已有文章，
    实现重入 / 旧批次回放。单独调用时退化为无历史比较。
    """
    if index is None:
        if isinstance(ctx, dict) and isinstance(ctx.get("dedup_index"), _Index):
            index = ctx["dedup_index"]
        else:
            loaded: list[Article] = []
            if isinstance(ctx, dict):
                loaded.extend(
                    a if isinstance(a, Article) else Article.from_doc(a)
                    for a in ctx.get("articles") or ()
                )
                if ctx.get("con") is not None and not loaded:
                    from . import store as _store

                    loaded.extend(Article.from_doc(doc) for _, doc in _store.all_records(ctx["con"]))
            loaded.extend(a if isinstance(a, Article) else Article.from_doc(a) for a in articles)
            index = _Index(loaded)
            if isinstance(ctx, dict):
                ctx["dedup_index"] = index

    ident = record.get("identity")
    if not isinstance(ident, dict) or not ident:
        ident = _identity.identity_of(record)
    time_ev = record.get("time_evidence")
    if not isinstance(time_ev, dict):
        time_ev = _publish_time.collect_time_evidence(record)
    retraction = record.get("retraction")
    if not isinstance(retraction, dict):
        from . import retract as _retract

        retraction = _retract.detect(record)

    source = _source_of(ident)
    url = ident.get("canonical_url") or ident.get("url") or ""
    aliases = _aliases_for(ident)

    # 1) 撤稿/更正声明
    if retraction.get("is_retraction"):
        article, matched_alias = _find_by_retraction_target(index, retraction, ident)
        if article is not None:
            _record_sources(article, ident, url, time_ev, "retraction_notice")
            for alias in aliases:
                article.add_alias(alias)
            # 回放撤稿前的“撤稿声明”不应覆盖已有的重发状态：只补事件证据。
            if _is_stale_observation(article, time_ev) and article.status not in {
                "retracted",
                "withdrawn",
            }:
                article.add_event(
                    _text(retraction.get("kind")) or "retraction",
                    _event_payload(
                        ident,
                        time_ev,
                        source,
                        url,
                        target=_text(retraction.get("target")),
                        kind=_text(retraction.get("kind")) or "retraction",
                        reason=_text(retraction.get("reason")),
                        declared_at=_text(retraction.get("declared_at")),
                        evidence=list(retraction.get("evidence") or []),
                        stale=True,
                    ),
                )
            else:
                _apply_retraction(article, ident, time_ev, retraction)
            index.add(article)
            return {
                "article": article.to_doc(),
                "match": {
                    "relation": "retraction",
                    "reason": "retraction_target",
                    "matched_alias": matched_alias,
                    "article_key": article.key,
                },
                "key": article.key,
            }

    # 2) 身份别名 / 规范 URL 找回
    article, matched_alias = index.lookup_aliases(aliases)
    via_alias = article is not None
    lookup_reason = "alias" if via_alias else ""
    if article is None:
        article, lookup_reason = index.lookup_content(ident, time_ev)

    if article is None:
        new_article = _article_for(ident, time_ev)
        if retraction.get("is_retraction"):
            _apply_retraction(new_article, ident, time_ev, retraction)
        index.add(new_article)
        return {
            "article": new_article.to_doc(),
            "match": {
                "relation": "new",
                "reason": "retraction_without_target" if retraction.get("is_retraction") else "no_match",
                "article_key": new_article.key,
            },
            "key": new_article.key,
        }

    relation, reason = _decide(article, ident, time_ev, via_alias=via_alias)
    payload = _event_payload(ident, time_ev, source, url, matched_alias=matched_alias)

    if relation == "distinct":
        new_article = _article_for(ident, time_ev)
        # 超出窗口的相同内容被判定为独立新文，但它是“基于旧文的重发”。
        # 保留指向旧文的证据；旧文的撤稿状态不迁移到新文。
        if reason in {"outside_window", "near_outside_window"}:
            new_article.add_event(
                "republication",
                _event_payload(
                    ident,
                    time_ev,
                    source,
                    url,
                    target=article.key,
                    kind="independent_reissue",
                    reason=reason,
                ),
            )
        if retraction.get("is_retraction"):
            _apply_retraction(new_article, ident, time_ev, retraction)
        index.add(new_article)
        return {
            "article": new_article.to_doc(),
            "match": {
                "relation": "new",
                "reason": reason,
                "article_key": new_article.key,
                "compared_with": article.key,
            },
            "key": new_article.key,
        }

    for alias in aliases:
        article.add_alias(alias)

    if relation == "same_url":
        if reason == "republication":
            # 撤稿后原 URL 重发：内容可能与旧版本相同，因此不保证新增版本，
            # 但必须留下重发事件并把状态恢复为 active。
            article.add_version(_version_of(ident, time_ev, source, url))
            _record_sources(article, ident, url, time_ev, "republication")
            article.add_event("republication", payload)
            article.status = "active"
            if retraction.get("is_retraction"):
                _apply_retraction(article, ident, time_ev, retraction)
        else:
            if _is_impoverished(article, ident):
                # 同一 URL 上的摘要副本：保留观察证据，不新增版本。
                _record_sources(article, ident, url, time_ev, "observed")
                if _observe_all(article, ident, url, time_ev):
                    article.add_event("observed", payload)
            else:
                added = article.add_version(_version_of(ident, time_ev, source, url))
                if added:
                    _record_sources(article, ident, url, time_ev, "revision")
                    article.add_event("revision", payload)
                else:
                    # 只有更新时间/抓取时间变化：保留观察证据，但不新增版本或文章。
                    _record_sources(article, ident, url, time_ev, "observed")
                    if _observe_all(article, ident, url, time_ev):
                        article.add_event("observed", payload)
    elif relation == "same_content":
        if reason == "republication":
            # 撤稿后重发：即使用旧内容，也必须恢复 active 并留下事件；
            # ``add_version`` 对相同摘要幂等，不会因此产生重复版本。
            article.add_version(_version_of(ident, time_ev, source, url))
            _record_sources(article, ident, url, time_ev, "republication")
            article.add_event("republication", payload)
            if article.status in {"retracted", "withdrawn"}:
                article.status = "active"
        elif reason == "stale_observation":
            # 旧批次回放：时间早于撤稿，只补观察证据，不复活文章。
            _record_sources(article, ident, url, time_ev, "observed")
            if _observe_all(article, ident, url, time_ev):
                article.add_event("observed", payload)
        else:
            _record_sources(article, ident, url, time_ev, "syndication")
            article.add_event("syndication", payload)
        if retraction.get("is_retraction"):
            _apply_retraction(article, ident, time_ev, retraction)
    else:  # near_content
        if _is_impoverished(article, ident):
            # 缺正文的副本不能当成“新版本”，否则 RSS 重抓会不断添加版本。
            _record_sources(article, ident, url, time_ev, "syndication")
            article.add_event("syndication", payload)
        else:
            added = article.add_version(_version_of(ident, time_ev, source, url))
            _record_sources(article, ident, url, time_ev, "revision")
            article.add_event("revision" if added else "syndication", payload)

    index.add(article)
    return {
        "article": article.to_doc(),
        "match": {
            "relation": relation,
            "reason": reason,
            "matched_alias": matched_alias,
            "lookup": lookup_reason,
            "article_key": article.key,
        },
        "key": article.key,
    }


def articles_from_store(con) -> list[Article]:
    """从 store 读回全部文章（旧批次回放/重入用）。"""
    from . import store as _store

    return [Article.from_doc(doc) for _, doc in _store.all_records(con)]
