"""单一职责：RSS 线索 → 正文页面补全（page_fill 阶段）。

RSS 条目只带摘要，正文可能藏在 canonical URL、重定向后的分页页面或附件里。
本模块只做这一件事：拿到一条记录里的 RSS 线索，选页面、抓正文、判完整性，
并把「线索 → 正文」的完整证据链落到项目自带的 SQLite，保证重复补全幂等、
失败可重试、页面更新后可接续，文章身份沿用上游契约（keyer.key_of）。

记录契约（process 输入/输出均为普通 dict，身份字段 id 不动）：
  输入线索（现代格式）:
    {"id": "a1", "rss_lead": {"link": "...", "title": "...", "summary": "..."}}
  兼容旧格式（见 _extract_lead）: 顶层 link/url、summary/description/excerpt、
    lead/rss/feed 信封等，都会规整成同一种线索。
  输出补全字段:
    body            选中的正文文本（未补全成功为 None，摘要绝不当正文）
    body_pages      正文实际取自的页面 URL（按分页顺序）
    canonical_url   最终选定的 canonical URL
    attachments     附件 [{"url", "text"}]
    fill_status     complete | truncated | shell | error
    fill_reason     选页/成败的人话理由
    fill_evidence   线索到正文的证据链（重定向链、分页、候选冲突、历次尝试）
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from .keyer import key_of
from . import store

# ---------------------------------------------------------------- 常量

STATE_TABLE = "page_fill_state"
MAX_REDIRECTS = 5
DEFAULT_MAX_PAGES = 10
DEFAULT_MAX_ATTEMPTS = 2
MIN_BODY_LEN = 20

REDIRECT_STATUSES = {301, 302, 303, 307, 308}
RETRY_STATUSES = {429, 500, 502, 503, 504}
BLOCK_STATUSES = {401, 403, 429, 503}
ATTACHMENT_SUFFIXES = {
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "zip", "rar", "7z", "txt", "csv", "epub",
}
BODY_HINTS = (
    "article-body", "article-content", "entry-content", "post-content",
    "main-content", "content-body", "story-body", "article__content",
)
SHELL_MARKERS = (
    "just a moment", "access denied", "enable javascript", "enable cookies",
    "请开启 cookie", "请启用 javascript", "访问验证", "安全验证",
    "人机验证", "验证码", "访问被拒绝",
)
TRUNCATE_MARKERS = (
    "阅读全文", "展开阅读全文", "继续阅读", "查看全文", "全文>>",
    "read more", "continue reading", "view full article",
)
NEXT_HINTS = ("next", "下一页", "下页", "›", "»", "older posts", "更旧")
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}
BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "li", "tr",
    "br", "blockquote", "pre", "table", "ul", "ol", "figure",
    "figcaption", "h1", "h2", "h3", "h4", "h5", "h6", "main", "aside",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 数据结构

@dataclass
class RawResponse:
    """一次 HTTP 交换的原样结果（不自动跟随重定向，重定向由本模块自己走链）。"""
    url: str
    status: int = 0
    headers: dict = field(default_factory=dict)
    text: str = ""
    error: str | None = None


@dataclass
class Lead:
    link: str
    title: str = ""
    summary: str = ""
    canonical_hint: str = ""
    legacy: bool = False


@dataclass
class Candidate:
    container: str
    text: str
    length: int
    sha256: str
    chosen: bool = False
    reason: str = ""


@dataclass
class PageInfo:
    url: str
    status: int
    canonical: str | None
    title: str
    candidates: list[Candidate]
    next_url: str | None
    attachments: list[dict]
    truncated: bool
    shell: bool
    etag: str | None
    last_modified: str | None
    content_sha256: str
    reason: str


# ---------------------------------------------------------------- 线索识别（含旧格式）

_LEAD_ENVELOPES = ("rss_lead", "lead", "rss", "feed")
_LINK_KEYS = ("link", "url", "canonical", "canonical_url", "href")
_SUMMARY_KEYS = ("summary", "description", "excerpt", "snippet", "digest")


def _first(mapping: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        v = mapping.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _extract_lead(record: dict) -> Lead | None:
    """从记录里找 RSS 线索；现代格式与历史旧格式走同一入口。"""
    envelope = None
    legacy = False
    for name in _LEAD_ENVELOPES:
        val = record.get(name)
        if isinstance(val, dict):
            envelope = val
            legacy = name != "rss_lead"
            break
    if envelope is None:
        # 旧格式：线索字段直接摊在记录顶层
        if any(k in record for k in _LINK_KEYS):
            envelope = record
            legacy = True
    if envelope is None:
        return None

    link = _first(envelope, _LINK_KEYS)
    if not link:
        return None
    scheme = urlparse(link).scheme
    if scheme and scheme not in ("http", "https"):
        return None

    lead = Lead(
        link=link,
        title=_first(envelope, ("title", "name", "headline")),
        summary=_first(envelope, tuple(_SUMMARY_KEYS)),
        canonical_hint=_first(envelope, ("canonical", "canonical_url")),
        legacy=legacy,
    )
    return lead


def _lead_fingerprint(lead: Lead) -> str:
    payload = json.dumps(
        {"link": lead.link, "title": lead.title,
         "summary": lead.summary, "canonical_hint": lead.canonical_hint},
        sort_keys=True, ensure_ascii=False,
    )
    return _sha256(payload)


# ---------------------------------------------------------------- 极简 HTML 树

class _Node:
    __slots__ = ("tag", "attrs", "parent", "items")

    def __init__(self, tag, attrs, parent):
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.items: list = []


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {}, None)
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, {k.lower(): (v or "") for k, v in attrs},
                     self._stack[-1])
        self._stack[-1].items.append(("n", node))
        if tag not in VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = _Node(tag, {k.lower(): (v or "") for k, v in attrs},
                     self._stack[-1])
        self._stack[-1].items.append(("n", node))

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data):
        self._stack[-1].items.append(("t", data))


def _walk(node: _Node):
    yield node
    for kind, item in node.items:
        if kind == "n":
            yield from _walk(item)


def _render(node: _Node) -> str:
    out: list[str] = []

    def go(n: _Node):
        for kind, item in n.items:
            if kind == "t":
                out.append(item)
            else:
                if item.tag in BLOCK_TAGS:
                    out.append("\n")
                go(item)
                if item.tag in BLOCK_TAGS:
                    out.append("\n")

    go(node)
    text = "".join(out)
    lines = [re.sub(r"[ \t\f\v]+", " ", ln).strip() for ln in text.split("\n")]
    text = "\n".join(ln for ln in lines if ln)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _node_label(node: _Node) -> str:
    nid = node.attrs.get("id", "")
    cls = node.attrs.get("class", "")
    label = node.tag
    if nid:
        label += f"#{nid}"
    if cls:
        label += "." + ".".join(cls.split())
    return label


def _is_descendant(node: _Node, ancestors: list[_Node]) -> bool:
    cur = node.parent
    while cur is not None:
        if cur in ancestors:
            return True
        cur = cur.parent
    return False


# ---------------------------------------------------------------- 页面分析

def _find_head_links(root: _Node):
    canonical = None
    next_url = None
    for node in _walk(root):
        if node.tag == "link":
            rel = node.attrs.get("rel", "").lower()
            href = node.attrs.get("href", "")
            if not href:
                continue
            if "canonical" in rel and canonical is None:
                canonical = href
            if "next" in rel.split() and next_url is None:
                next_url = href
        elif node.tag == "meta":
            prop = node.attrs.get("property", "").lower()
            if prop == "og:url" and canonical is None:
                canonical = node.attrs.get("content", "")
    return canonical, next_url


def _find_anchor_next(root: _Node) -> str | None:
    for node in _walk(root):
        if node.tag != "a":
            continue
        rel = node.attrs.get("rel", "").lower()
        cls = node.attrs.get("class", "").lower()
        nid = node.attrs.get("id", "").lower()
        label = f"{rel} {cls} {nid} {_render(node)}".lower()
        if "next" in rel.split() or any(h in label for h in NEXT_HINTS):
            href = node.attrs.get("href", "")
            if href and not href.startswith("#"):
                return href
    return None


def _find_attachments(root: _Node, base_url: str) -> list[dict]:
    found = {}
    for node in _walk(root):
        if node.tag != "a":
            continue
        href = node.attrs.get("href", "")
        if not href:
            continue
        absolute = urljoin(base_url, href)
        suffix = urlparse(absolute).path.rsplit(".", 1)[-1].lower()
        cls = node.attrs.get("class", "").lower()
        rel = node.attrs.get("rel", "").lower()
        is_attach = (suffix in ATTACHMENT_SUFFIXES
                     or "attachment" in cls or "attachment" in rel)
        if is_attach and absolute not in found:
            found[absolute] = {"url": absolute, "text": _render(node)[:120]}
    return list(found.values())


def _find_candidates(root: _Node) -> list[_Node]:
    picked: list[_Node] = []
    for node in _walk(root):
        if node.tag == "article":
            picked.append(node)
            continue
        ident = (node.attrs.get("id", "") + " "
                 + node.attrs.get("class", "")).lower()
        if any(hint in ident for hint in BODY_HINTS):
            picked.append(node)
    # 嵌套候选去重：祖先已入选则丢掉后代，避免同一段正文重复计数
    return [n for n in picked if not _is_descendant(n, picked)]


def _has_truncate_marker(root: _Node, body_text: str) -> bool:
    low = body_text.lower()
    if any(m in low for m in TRUNCATE_MARKERS):
        return True
    tail = body_text[-40:].strip()
    if tail.endswith(("…", "...", "。。。")):
        return True
    for node in _walk(root):
        if node.tag != "a":
            continue
        label = (node.attrs.get("class", "") + " "
                 + node.attrs.get("id", "")).lower()
        if "read-more" in label or "readmore" in label or "more-link" in label:
            return True
    return False


def _is_shell(root: _Node, status: int, full_text: str, title: str) -> bool:
    if status in BLOCK_STATUSES:
        return True
    blob = f"{title}\n{full_text}".lower()
    if any(m in blob for m in SHELL_MARKERS):
        return True
    if len(full_text.strip()) < MIN_BODY_LEN:
        return True
    return False


def analyze_page(url: str, resp: RawResponse) -> PageInfo:
    """把一份 HTTP 响应分析成选页所需的结构化页面信息（纯函数，便于离线回放）。"""
    html = resp.text or ""
    builder = _TreeBuilder()
    try:
        builder.feed(html)
    except Exception:  # 容错：破损 HTML 也尽量给出已解析部分
        pass
    root = builder.root
    title = ""
    for node in _walk(root):
        if node.tag == "title":
            title = _render(node)
            break

    canonical, link_next = _find_head_links(root)
    if canonical:
        canonical = urljoin(url, canonical)
    next_url = link_next or _find_anchor_next(root)
    if next_url:
        next_url = urljoin(url, next_url)

    candidate_nodes = _find_candidates(root)
    candidates: list[Candidate] = []
    for node in candidate_nodes:
        text = _render(node)
        candidates.append(Candidate(
            container=_node_label(node),
            text=text,
            length=len(text),
            sha256=_sha256(text),
        ))
    # 选页规则：同页多候选取正文最长者，平局取文档顺序在先者；全部候选留证可回查
    chosen_idx = -1
    if candidates:
        chosen_idx = max(
            range(len(candidates)),
            key=lambda i: candidates[i].length,
        )
        for i, cand in enumerate(candidates):
            cand.chosen = i == chosen_idx
            cand.reason = (
                f"longest_candidate length={cand.length}" if cand.chosen
                else f"rejected_shorter length={cand.length}"
            )

    full_text = _render(root)
    body_text = candidates[chosen_idx].text if chosen_idx >= 0 else ""
    shell = _is_shell(root, resp.status, full_text, title)
    truncated = bool(body_text) and _has_truncate_marker(root, body_text)

    if shell:
        reason = f"shell_or_anti_bot status={resp.status} title={title!r}"
    elif not candidates:
        reason = "no_body_container"
    elif truncated:
        reason = "truncation_marker_found"
    else:
        reason = f"body_selected container={candidates[chosen_idx].container}"

    headers = {k.lower(): v for k, v in resp.headers.items()}
    return PageInfo(
        url=url,
        status=resp.status,
        canonical=canonical,
        title=title,
        candidates=candidates,
        next_url=next_url,
        attachments=_find_attachments(root, url),
        truncated=truncated,
        shell=shell,
        etag=headers.get("etag"),
        last_modified=headers.get("last-modified"),
        content_sha256=_sha256(html),
        reason=reason,
    )


# ---------------------------------------------------------------- 抓取（可注入）

class HttpFetcher:
    """默认抓取器：优先 httpx，缺失时退回标准库 urllib；均不自动跟随重定向。"""

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout
        self._httpx = None
        try:
            import httpx  # type: ignore
            self._httpx = httpx
        except ImportError:
            self._httpx = None

    def get(self, url: str) -> RawResponse:
        if self._httpx is not None:
            return self._get_httpx(url)
        return self._get_urllib(url)

    def _get_httpx(self, url: str) -> RawResponse:
        try:
            with self._httpx.Client(timeout=self.timeout,
                                    follow_redirects=False) as client:
                r = client.get(url)
                return RawResponse(
                    url=str(r.url), status=r.status_code,
                    headers={k.lower(): v for k, v in r.headers.items()},
                    text=r.text,
                )
        except Exception as exc:  # 网络层失败统一成 error 响应
            return RawResponse(url=url, error=f"{type(exc).__name__}: {exc}")

    def _get_urllib(self, url: str) -> RawResponse:
        import urllib.error
        import urllib.request

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise _RedirectSignal(code, newurl, headers)

        class _RedirectSignal(Exception):
            def __init__(self, code, location, headers):
                super().__init__(location)
                self.code = code
                self.location = location
                self.headers = headers

        opener = urllib.request.build_opener(_NoRedirect())
        try:
            resp = opener.open(url, timeout=self.timeout)
            status = getattr(resp, "status", None) or resp.getcode()
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return RawResponse(url=url, status=status, headers=headers,
                               text=resp.read().decode("utf-8", "replace"))
        except _RedirectSignal as sig:
            headers = {k.lower(): v for k, v in sig.headers.items()}
            headers["location"] = urljoin(url, sig.location)
            return RawResponse(url=url, status=sig.code, headers=headers)
        except urllib.error.HTTPError as exc:
            headers = {k.lower(): v for k, v in exc.headers.items()}
            text = ""
            try:
                text = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            return RawResponse(url=url, status=exc.code, headers=headers,
                               text=text)
        except Exception as exc:
            return RawResponse(url=url, error=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- 补全主流程

def _fetch_once(fetcher, url: str, seen: list[str]) -> tuple[RawResponse, list[dict]]:
    """跟随重定向链直到首个非重定向响应，逐跳留证。"""
    chain: list[dict] = []
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if current in seen:
            resp = RawResponse(url=current,
                               error=f"CircularRedirect: {current}")
            return resp, chain
        seen.append(current)
        resp = fetcher.get(current)
        if resp.error:
            return resp, chain
        if resp.status in REDIRECT_STATUSES:
            location = resp.headers.get("location") or \
                resp.headers.get("Location")
            if not location:
                resp.error = f"RedirectWithoutLocation status={resp.status}"
                return resp, chain
            nxt = urljoin(current, location)
            chain.append({"from": current, "status": resp.status,
                          "to": nxt, "location": location})
            current = nxt
            continue
        resp.url = current
        return resp, chain
    return (RawResponse(url=current,
                        error=f"TooManyRedirects >{MAX_REDIRECTS}"), chain)


def _page_evidence(page: PageInfo) -> dict:
    return {
        "url": page.url,
        "status": page.status,
        "canonical": page.canonical,
        "canonical_follow": None,
        "title": page.title,
        "etag": page.etag,
        "last_modified": page.last_modified,
        "content_sha256": page.content_sha256,
        "truncated": page.truncated,
        "shell": page.shell,
        "next_url": page.next_url,
        "reason": page.reason,
        "candidates": [
            {
                "container": c.container,
                "length": c.length,
                "sha256": c.sha256,
                "chosen": c.chosen,
                "reason": c.reason,
            } for c in page.candidates
        ],
        "attachments": page.attachments,
    }


def _page_signature(page: PageInfo | None, resp: RawResponse) -> str:
    if page is not None:
        return "|".join(filter(None, (
            f"status={page.status}", page.etag, page.last_modified,
            page.content_sha256,
        )))
    if resp.error:
        return f"error:{resp.error}"
    return f"status={resp.status}:{_sha256(resp.text or '')}"


def _crawl(lead: Lead, fetcher, max_pages: int, max_attempts: int) -> dict:
    """一次完整补全尝试：重定向 → canonical → 分页正文 → 附件，产出证据。"""
    attempt = {
        "attempted_at": _now(),
        "lead_link": lead.link,
        "lead_fingerprint": _lead_fingerprint(lead),
        "redirect_chain": [],
        "pages": [],
        "error": None,
        "result": None,
        "selection_reason": "",
        "page_signature": None,
    }
    seen: list[str] = []
    last_resp = RawResponse(url=lead.link, error="not_fetched")
    last_page: PageInfo | None = None
    last_chain: list[dict] = []

    for try_no in range(1, max_attempts + 1):
        seen = []
        resp, chain = _fetch_once(fetcher, lead.link, seen)
        if chain:
            # 失败重试也保留最后一次走出来的重定向链，证据不丢
            last_chain = chain
        attempt["redirect_chain"] = last_chain
        last_resp = resp
        if resp.error:
            if try_no < max_attempts:
                continue
            attempt["error"] = {"type": "network", "message": resp.error,
                                "url": resp.url}
            attempt["result"] = "error"
            attempt["selection_reason"] = f"fetch_failed: {resp.error}"
            attempt["page_signature"] = _page_signature(None, resp)
            return attempt
        if resp.status in RETRY_STATUSES and try_no < max_attempts:
            continue
        break

    if last_resp.error:
        attempt["error"] = {"type": "network", "message": last_resp.error,
                            "url": last_resp.url}
        attempt["result"] = "error"
        attempt["selection_reason"] = f"fetch_failed: {last_resp.error}"
        attempt["page_signature"] = _page_signature(None, last_resp)
        return attempt

    # 页面遍历：canonical 选页 → 分页正文 → 附件
    page_url = last_resp.url
    bodies: list[str] = []
    body_pages: list[str] = []
    attachments: list[dict] = []
    attach_seen: set[str] = set()
    canonical = lead.canonical_hint or None
    hit_max_pages = False
    visited: set[str] = set()
    page_idx = 0

    while page_idx < max_pages:
        if page_idx > 0:
            resp = fetcher.get(page_url)
            if resp.error:
                # 带失败返回前先固化已抓到的前序分页正文，证据不丢
                attempt["body"] = "\n\n".join(bodies) if bodies else None
                attempt["body_pages"] = body_pages
                attempt["canonical_url"] = canonical
                attempt["attachments"] = attachments
                attempt["error"] = {"type": "network",
                                    "message": resp.error, "url": page_url}
                attempt["result"] = "error"
                attempt["selection_reason"] = f"page_fetch_failed: {resp.error}"
                attempt["page_signature"] = (
                    attempt["page_signature"] or _page_signature(None, resp))
                return attempt
            resp.url = page_url
        info = analyze_page(page_url, resp)
        if page_idx == 0:
            last_page = info
            attempt["page_signature"] = _page_signature(info, resp)
        visited.add(page_url)
        ev = _page_evidence(info)
        attempt["pages"].append(ev)

        if info.canonical and not canonical:
            canonical = info.canonical
        for att in info.attachments:
            if att["url"] not in attach_seen:
                attach_seen.add(att["url"])
                attachments.append(att)

        chosen = next((c for c in info.candidates if c.chosen), None)
        page_has_body = bool(chosen and chosen.text)
        if page_has_body:
            bodies.append(chosen.text)
            body_pages.append(page_url)

        # canonical 选页：空壳/无正文容器且 canonical 指向未访问页面时，
        # 跟随 canonical 继续选页，原壳页与跟随理由都留在证据链里
        follow = None
        if not page_has_body and info.canonical \
                and info.canonical not in visited:
            follow = info.canonical
            ev["canonical_follow"] = follow
        elif info.shell and canonical and canonical not in visited:
            follow = canonical
            ev["canonical_follow"] = follow

        if follow is not None:
            page_url = follow
            page_idx += 1
            continue

        if info.shell:
            attempt["result"] = "shell"
            attempt["selection_reason"] = info.reason
            break
        if info.truncated:
            attempt["result"] = "truncated"
            attempt["selection_reason"] = info.reason
            ev["body_taken"] = page_has_body
            break
        if not info.next_url or info.next_url == page_url:
            attempt["result"] = "complete" if bodies else "shell"
            attempt["selection_reason"] = (
                "pagination_ended_with_full_body" if bodies
                else "no_body_on_any_page"
            )
            break
        if page_idx == max_pages - 1:
            hit_max_pages = True
            attempt["result"] = "truncated"
            attempt["selection_reason"] = (
                f"max_pages={max_pages}_reached_next={info.next_url}")
            break
        page_url = info.next_url
        page_idx += 1
    else:  # pragma: no cover - while/else 保险分支
        hit_max_pages = True
        attempt["result"] = "truncated"
        attempt["selection_reason"] = f"max_pages={max_pages}_reached"

    attempt["body"] = "\n\n".join(bodies) if bodies else None
    attempt["body_pages"] = body_pages
    attempt["canonical_url"] = canonical
    attempt["attachments"] = attachments
    attempt["hit_max_pages"] = hit_max_pages
    return attempt


# ---------------------------------------------------------------- 状态持久化

def _ensure_table(con: sqlite3.Connection) -> None:
    con.execute(
        f"CREATE TABLE IF NOT EXISTS {STATE_TABLE} ("
        "article_key TEXT PRIMARY KEY, "
        "lead_fp TEXT NOT NULL, "
        "data TEXT NOT NULL, "
        "updated_at TEXT NOT NULL)")


def _load_state(con: sqlite3.Connection, article_key: str) -> dict | None:
    row = con.execute(
        f"SELECT data FROM {STATE_TABLE} WHERE article_key=?",
        (article_key,)).fetchone()
    return json.loads(row[0]) if row else None


def _save_state(con: sqlite3.Connection, article_key: str,
                lead_fp: str, state: dict) -> None:
    con.execute(
        f"INSERT INTO {STATE_TABLE}(article_key,lead_fp,data,updated_at) "
        "VALUES(?,?,?,?) ON CONFLICT(article_key) DO UPDATE SET "
        "lead_fp=excluded.lead_fp, data=excluded.data, "
        "updated_at=excluded.updated_at",
        (article_key, lead_fp,
         json.dumps(state, ensure_ascii=False, sort_keys=True), _now()))
    con.commit()


def _apply_state(record: dict, state: dict) -> dict:
    rec = dict(record)
    rec["body"] = state.get("body")
    rec["body_pages"] = list(state.get("body_pages", ()))
    rec["canonical_url"] = state.get("canonical_url")
    rec["attachments"] = [dict(a) for a in state.get("attachments", ())]
    rec["fill_status"] = state.get("status")
    rec["fill_reason"] = state.get("reason", "")
    rec["fill_evidence"] = json.loads(json.dumps(
        state.get("evidence", {}), ensure_ascii=False))
    rec["_stage_page_fill"] = (
        "ok" if state.get("status") == "complete"
        else f"failed:{state.get('status')}")
    return rec


# ---------------------------------------------------------------- 阶段入口

def process(record: dict, *, con: sqlite3.Connection | None = None,
            fetcher=None, max_pages: int = DEFAULT_MAX_PAGES,
            max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> dict:
    """对单条记录执行 RSS 线索 → 正文页面补全。

    无线索的记录原样透传（只打阶段标记），身份字段不变；
    有线索则复用/更新 SQLite 中的补全状态：
      - 同线索且已 complete：直接回灌状态，不发任何请求（幂等）；
      - 同线索但曾失败：只做一次探测，页面签名未变则保留原证据链，
        页面已更新则追加新尝试（失败后页面更新可接续）；
      - 线索变更：重新补全，历史尝试保留在证据链里可回查。
    """
    lead = _extract_lead(record)
    if lead is None:
        rec = dict(record)
        rec.setdefault("_stage_page_fill", "no_lead")
        return rec

    own_con = con is None
    con = con or store.connect()
    try:
        _ensure_table(con)
        article_key = key_of(record)  # 文章身份沿用上游契约
        lead_fp = _lead_fingerprint(lead)
        state = _load_state(con, article_key)

        if state is not None and state.get("lead_fp") == lead_fp \
                and state.get("status") == "complete":
            return _apply_state(record, state)

        fetcher = fetcher or HttpFetcher()
        attempt = _crawl(lead, fetcher, max_pages, max_attempts)

        if state is None:
            state = {
                "article_key": article_key,
                "lead_fp": lead_fp,
                "legacy_lead": lead.legacy,
                "status": attempt["result"],
                "reason": attempt["selection_reason"],
                "body": attempt.get("body"),
                "body_pages": attempt.get("body_pages", []),
                "canonical_url": attempt.get("canonical_url"),
                "attachments": attempt.get("attachments", []),
                "evidence": {
                    "lead": {
                        "link": lead.link,
                        "title": lead.title,
                        "summary_present": bool(lead.summary),
                        "canonical_hint": lead.canonical_hint,
                        "legacy_format": lead.legacy,
                        "fingerprint": lead_fp,
                    },
                    "attempts": [attempt],
                },
            }
        else:
            last_sig = state["evidence"]["attempts"][-1].get(
                "page_signature")
            same_failure = (
                state.get("lead_fp") == lead_fp
                and state.get("status") != "complete"
                and attempt.get("page_signature") is not None
                and attempt["page_signature"] == last_sig)
            if same_failure:
                # 页面没更新：不追加重复尝试，证据链保持稳定（幂等）
                return _apply_state(record, state)
            state["lead_fp"] = lead_fp
            state["legacy_lead"] = state.get("legacy_lead") or lead.legacy
            state["evidence"].setdefault("attempts", []).append(attempt)
            if attempt["result"] == "complete":
                state["status"] = "complete"
                state["body"] = attempt.get("body")
                state["body_pages"] = attempt.get("body_pages", [])
                state["canonical_url"] = attempt.get("canonical_url")
                state["attachments"] = attempt.get("attachments", [])
            else:
                state["status"] = attempt["result"]
                state["body"] = attempt.get("body") or state.get("body")
                state["body_pages"] = (
                    attempt.get("body_pages") or state.get("body_pages", []))
                state["attachments"] = list({
                    a["url"]: a
                    for a in (*state.get("attachments", []),
                              *attempt.get("attachments", []))
                }.values())
            state["reason"] = attempt["selection_reason"]

        _save_state(con, article_key, lead_fp, state)
        return _apply_state(record, state)
    finally:
        if own_con:
            con.close()


def process_batch(records, *, con: sqlite3.Connection | None = None,
                  fetcher=None, max_pages: int = DEFAULT_MAX_PAGES,
                  max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> list[dict]:
    """逐条补全一批记录；每条独立落盘提交，半批中断后重启可接续。"""
    own_con = con is None
    con = con or store.connect()
    try:
        return [
            process(rec, con=con, fetcher=fetcher,
                    max_pages=max_pages, max_attempts=max_attempts)
            for rec in records
        ]
    finally:
        if own_con:
            con.close()
