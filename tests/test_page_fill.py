"""page_fill 离线序列验证：重定向链、截断正文、失败后页面更新、幂等、
半批提交重启、候选冲突回查、旧线索格式、反爬/空壳、附件与 canonical 选页。

不依赖网络与第三方库：抓取器全部使用脚本化 MockFetcher 回放页面序列。
同时兼容 pytest 与 tests/run_tests.py（标准库 runner）。
"""
import json
import os
import sqlite3
import tempfile

from src.newswire import page_fill, pipeline, store


# ---------------------------------------------------------------- 测试夹具

class MockFetcher:
    """按 URL 脚本化回放响应；同一 URL 可给一串随时间演化的响应（页面更新）。"""

    def __init__(self, routes):
        self.routes = {}
        for u, specs in routes.items():
            self.routes[u] = specs if isinstance(specs, list) else [specs]
        self.calls = {}

    def get(self, url):
        idx = self.calls.get(url, 0)
        self.calls[url] = idx + 1
        specs = self.routes.get(url)
        if specs is None:
            return page_fill.RawResponse(url=url, error=f"NotMapped: {url}")
        spec = specs[min(idx, len(specs) - 1)]
        if "error" in spec:
            return page_fill.RawResponse(url=url, error=spec["error"])
        return page_fill.RawResponse(
            url=url,
            status=spec.get("status", 200),
            headers={k.lower(): v for k, v in spec.get("headers", {}).items()},
            text=spec.get("html", spec.get("text", "")),
        )


def _page(parts, *, canonical=None, next_url=None, title="T", extra=""):
    canon = (f'<link rel="canonical" href="{canonical}">'
             if canonical else "")
    nxt = (f'<a rel="next" href="{next_url}">下一页 »</a>'
           if next_url else "")
    bodies = "".join(
        f'<div class="article-body" id="sec{i}">{p}</div>'
        for i, p in enumerate(parts))
    return (f"<!doctype html><html><head><title>{title}</title>{canon}"
            f"</head><body>{bodies}{nxt}{extra}</body></html>")


def _lead(link, **kw):
    d = {"id": kw.pop("id", "art-1"),
         "rss_lead": {"link": link, "title": "标题",
                      "summary": "RSS 摘要，绝不能当正文"}}
    d["rss_lead"].update(kw)
    return d


def _new_db():
    tmp = tempfile.mkdtemp(prefix="pf_")
    path = os.path.join(tmp, "state.db")
    con = sqlite3.connect(path)
    page_fill._ensure_table(con)
    return path, con


def _state_of(con, key):
    row = con.execute(
        "SELECT data FROM page_fill_state WHERE article_key=?",
        (key,)).fetchone()
    return json.loads(row[0]) if row else None


# ---------------------------------------------------------------- 1. 重定向链 + 分页正文

def test_redirect_chain_and_paginated_body():
    routes = {
        "https://news.test/old": {"status": 301,
                                  "headers": {"Location": "https://news.test/mid"}},
        "https://news.test/mid": {"status": 302,
                                  "headers": {"Location": "https://news.test/a"}},
        "https://news.test/a": {"html": _page(
            ["第一段正文内容，长度足够长，明显不是摘要。"],
            canonical="https://news.test/canonical",
            next_url="https://news.test/a?page=2")},
        "https://news.test/a?page=2": {"html": _page(
            ["第二段正文内容，分页续上的完整段落，长度同样足够长。"])},
    }
    _, con = _new_db()
    fetcher = MockFetcher(routes)
    out = page_fill.process(_lead("https://news.test/old"),
                            con=con, fetcher=fetcher)

    assert out["id"] == "art-1"  # 身份沿用上游契约
    assert out["fill_status"] == "complete"
    assert "第一段" in out["body"] and "第二段" in out["body"]
    assert out["body"] != "RSS 摘要，绝不能当正文"  # 摘要永远不进正文
    assert out["body_pages"] == ["https://news.test/a",
                                 "https://news.test/a?page=2"]
    assert out["canonical_url"] == "https://news.test/canonical"

    ev = out["fill_evidence"]
    chain = ev["attempts"][0]["redirect_chain"]
    assert [(h["from"], h["to"]) for h in chain] == [
        ("https://news.test/old", "https://news.test/mid"),
        ("https://news.test/mid", "https://news.test/a"),
    ]
    assert [p["url"] for p in ev["attempts"][0]["pages"]] == out["body_pages"]
    assert ev["lead"]["link"] == "https://news.test/old"
    assert ev["lead"]["summary_present"] is True


# ---------------------------------------------------------------- 2. 截断正文

def test_truncated_body_is_not_complete():
    html = _page(["这是一段被截断的正文，里面带着阅读全文的标记。"
                  '<a class="read-more" href="/full">阅读全文</a>'])
    _, con = _new_db()
    out = page_fill.process(
        _lead("https://news.test/trunc"),
        con=con, fetcher=MockFetcher({"https://news.test/trunc": {"html": html}}))
    assert out["fill_status"] == "truncated"
    assert out["body"] is not None and "截断" in out["body"]
    assert "truncation" in out["fill_reason"]
    assert out["_stage_page_fill"] == "failed:truncated"

    # 没有正文容器时，即便有摘要也不能拿摘要充正文
    empty = "<html><body><p>hi</p></body></html>"
    out2 = page_fill.process(
        _lead("https://news.test/empty", id="art-2"),
        con=con, fetcher=MockFetcher(
            {"https://news.test/empty": {"html": empty}}))
    assert out2["body"] is None
    assert out2["fill_status"] in ("shell", "truncated")


def test_max_pages_truncation():
    routes = {}
    for i in range(4):
        nxt = f"https://news.test/p{i + 1}" if i < 3 else None
        routes[f"https://news.test/p{i}"] = {
            "html": _page([f"第{i}页正文段落，长度足够长，凑过最小长度门槛。"],
                          next_url=nxt)}
    _, con = _new_db()
    out = page_fill.process(
        _lead("https://news.test/p0", id="art-max"),
        con=con, fetcher=MockFetcher(routes), max_pages=2)
    assert out["fill_status"] == "truncated"
    assert "max_pages=2" in out["fill_reason"]
    assert len(out["body_pages"]) == 2


# ---------------------------------------------------------------- 3. 失败 → 页面更新 → 重试接续

def test_failure_then_page_update_retry():
    url = "https://news.test/flaky"
    fetcher = MockFetcher({url: [
        {"error": "ConnectionError: reset by peer"},
        {"error": "ConnectionError: reset by peer"},
        {"html": _page(["页面恢复后的完整正文，长度足够长，与失败时签名不同。"])},
    ]})
    _, con = _new_db()
    rec = _lead(url)

    first = page_fill.process(rec, con=con, fetcher=fetcher, max_attempts=1)
    assert first["fill_status"] == "error"
    state = _state_of(con, "art-1")
    assert len(state["evidence"]["attempts"]) == 1
    calls_after_first = dict(fetcher.calls)

    # 页面未更新：重试只做探测，证据链不追加、输出保持稳定（幂等）
    second = page_fill.process(rec, con=con, fetcher=fetcher, max_attempts=1)
    state2 = _state_of(con, "art-1")
    assert second["fill_status"] == "error"
    assert len(state2["evidence"]["attempts"]) == 1
    assert fetcher.calls[url] == calls_after_first[url] + 1  # 仅一次探测
    assert second["fill_evidence"] == first["fill_evidence"]

    # 页面更新：追加新尝试，补全成功，旧失败尝试仍留证可回查
    third = page_fill.process(rec, con=con, fetcher=fetcher, max_attempts=1)
    state3 = _state_of(con, "art-1")
    assert third["fill_status"] == "complete"
    assert "完整正文" in third["body"]
    assert len(state3["evidence"]["attempts"]) == 2
    assert state3["evidence"]["attempts"][0]["result"] == "error"
    assert state3["evidence"]["attempts"][1]["result"] == "complete"


# ---------------------------------------------------------------- 4. 重复补全幂等

def test_repeated_completion_is_idempotent_and_refetches_nothing():
    url = "https://news.test/once"
    fetcher = MockFetcher({url: {"html": _page(["完整正文内容，长度足够长，这是一个超过二十个字符的完整段落。"])}})
    _, con = _new_db()
    rec = _lead(url)

    a = page_fill.process(rec, con=con, fetcher=fetcher)
    calls = dict(fetcher.calls)
    b = page_fill.process(rec, con=con, fetcher=fetcher)
    c = page_fill.process(rec, con=con, fetcher=fetcher)

    assert fetcher.calls == calls  # complete 后零额外请求
    assert a["body"] == b["body"] == c["body"]
    assert a["fill_evidence"] == b["fill_evidence"] == c["fill_evidence"]
    assert con.execute("SELECT COUNT(*) FROM page_fill_state").fetchone()[0] == 1
    rows = con.execute("SELECT data FROM page_fill_state").fetchall()
    assert json.loads(rows[0][0])["status"] == "complete"


# ---------------------------------------------------------------- 5. 半批提交后重启

def test_half_batch_commit_then_restart():
    routes = {f"https://news.test/b{i}": {
        "html": _page([f"第{i}篇的完整正文段落，长度足够长，超过空壳判定门槛。"])}
        for i in range(3)}
    # 首批里第三篇持续失败，模拟半批中断/部分失败
    routes["https://news.test/b2"] = {"error": "TimeoutError"}
    db_path, con = _new_db()
    records = [_lead(f"https://news.test/b{i}", id=f"b{i}") for i in range(3)]

    page_fill.process_batch(records, con=con, fetcher=MockFetcher(routes))
    snapshot = {k: _state_of(con, k) for k in ("b0", "b1", "b2")}
    assert snapshot["b0"]["status"] == "complete"
    assert snapshot["b2"]["status"] == "error"

    # “重启”：全新连接打开同一个库；修复第三篇后整批重跑
    con.commit()
    con.close()
    con2 = sqlite3.connect(db_path)
    routes["https://news.test/b2"] = {
        "html": _page(["第三篇恢复后的完整正文，长度足够长，已超过空壳判定的门槛。"])}
    fetcher2 = MockFetcher(routes)
    page_fill.process_batch(records, con=con2, fetcher=fetcher2)

    for key in ("b0", "b1"):
        again = _state_of(con2, key)
        assert again["body"] == snapshot[key]["body"]
        assert again["evidence"] == snapshot[key]["evidence"]
        assert fetcher2.calls.get(f"https://news.test/{key}") is None
    fixed = _state_of(con2, "b2")
    assert fixed["status"] == "complete"
    assert len(fixed["evidence"]["attempts"]) == 2

    # 再跑整批：全部幂等，内容与证据链逐字节一致
    fetcher3 = MockFetcher(routes)
    page_fill.process_batch(records, con=con2, fetcher=fetcher3)
    for key in ("b0", "b1", "b2"):
        final = _state_of(con2, key)
        assert final["status"] == "complete" if key == "b2" else True
        if key != "b2":
            assert final["evidence"] == snapshot[key]["evidence"]
    assert fetcher3.calls == {}
    con2.close()


# ---------------------------------------------------------------- 6. 多候选冲突可回查

def test_conflicting_candidates_choice_is_reviewable():
    html = (
        '<html><body>'
        '<div class="article-body" id="short">短正文。</div>'
        '<article id="long">'
        "<p>" + "长候选正文内容，" * 20 + "</p>"
        '<div class="article-body nested">嵌套的重复段落，'
        "应当作为后代被去重，不参与冲突。</div>"
        "</article></body></html>")
    _, con = _new_db()
    out = page_fill.process(
        _lead("https://news.test/conflict", id="c1"),
        con=con, fetcher=MockFetcher(
            {"https://news.test/conflict": {"html": html}}))
    assert out["fill_status"] == "complete"
    cands = out["fill_evidence"]["attempts"][0]["pages"][0]["candidates"]
    containers = [c["container"] for c in cands]
    assert any(c.startswith("article#long") for c in containers)
    assert any(c.startswith("div#short.article-body") for c in containers)
    # 嵌套候选已作为后代去重，不参与冲突
    assert all("nested" not in c for c in containers)
    chosen = [c for c in cands if c["chosen"]]
    rejected = [c for c in cands if not c["chosen"]]
    assert len(chosen) == 1 and chosen[0]["length"] == max(
        c["length"] for c in cands)
    assert all("longest" in c["reason"] for c in chosen)
    assert all("rejected" in c["reason"] for c in rejected)
    assert all(c["sha256"] for c in cands)  # 每个候选都有指纹可回查
    assert "长候选正文内容" in out["body"]


# ---------------------------------------------------------------- 7. 旧线索格式仍可进入流程

def test_legacy_lead_formats_enter_pipeline():
    page = {"html": _page(["旧格式线索抓回来的完整正文，长度足够长，段落字符数超过二十。"])}
    _, con = _new_db()
    fetcher = MockFetcher({"https://news.test/legacy": page})

    old_flat = {"id": "lg-1", "link": "https://news.test/legacy",
                "description": "旧顶层摘要"}
    old_env = {"id": "lg-2", "lead": {"url": "https://news.test/legacy",
                                      "excerpt": "旧信封摘要"}}
    old_feed = {"id": "lg-3", "feed": {"href": "https://news.test/legacy",
                                       "title": "T"}}
    for rec in (old_flat, old_env, old_feed):
        out = page_fill.process(rec, con=con, fetcher=fetcher)
        assert out["fill_status"] == "complete"
        assert out["fill_evidence"]["lead"]["legacy_format"] is True
        assert "完整正文" in out["body"]
        assert out["id"] == rec["id"]

    # 现代格式标记为非 legacy
    modern = page_fill.process(
        _lead("https://news.test/legacy", id="lg-4"),
        con=con, fetcher=fetcher)
    assert modern["fill_evidence"]["lead"]["legacy_format"] is False


# ---------------------------------------------------------------- 8. 反爬 / 空壳 / 附件

def test_anti_bot_shell_and_attachments():
    challenge = ('<html><head><title>Just a moment...</title></head>'
                 '<body><p>请启用 JavaScript 并开启 Cookie 后继续访问。</p>'
                 "</body></html>")
    _, con = _new_db()
    blocked = page_fill.process(
        _lead("https://news.test/blocked", id="bl"),
        con=con, fetcher=MockFetcher({
            "https://news.test/blocked": {"status": 403, "html": challenge}}))
    assert blocked["fill_status"] == "shell"
    assert blocked["body"] is None
    assert "shell" in blocked["fill_reason"]

    with_pdf = _page(
        ["带附件页面的完整正文，长度足够长，确保被判为完整文章段落。"],
        extra='<a href="/files/report.pdf">报告 PDF</a>'
              '<a class="attachment" href="/files/data.xlsx">表格</a>'
              '<a href="/about">普通内链不算附件</a>')
    ok = page_fill.process(
        _lead("https://news.test/with-attach", id="at"),
        con=con, fetcher=MockFetcher({
            "https://news.test/with-attach": {"html": with_pdf}}))
    assert ok["fill_status"] == "complete"
    urls = {a["url"] for a in ok["attachments"]}
    assert urls == {"https://news.test/files/report.pdf",
                    "https://news.test/files/data.xlsx"}
    assert all(a["text"] for a in ok["attachments"])


def test_empty_shell_page():
    _, con = _new_db()
    out = page_fill.process(
        _lead("https://news.test/blank", id="bk"),
        con=con, fetcher=MockFetcher({
            "https://news.test/blank": {"html": "<html><body></body></html>"}}))
    assert out["fill_status"] == "shell"
    assert out["body"] is None


# ---------------------------------------------------------------- 9. canonical 选页、状态码重试、线索变更、重定向环

def test_canonical_url_selected_when_landing_page_is_shell():
    shell_html = ('<html><head><title>Just a moment...</title>'
                  '<link rel="canonical" href="https://news.test/real">'
                  "</head><body><p>请启用 JavaScript 后继续。</p></body></html>")
    routes = {
        "https://news.test/shell": {"html": shell_html},
        "https://news.test/real": {"html": _page(
            ["canonical 指向的真实正文页面，段落足够长，判定为完整文章。"])},
    }
    _, con = _new_db()
    out = page_fill.process(
        _lead("https://news.test/shell", id="cx"),
        con=con, fetcher=MockFetcher(routes))
    assert out["fill_status"] == "complete"
    assert out["canonical_url"] == "https://news.test/real"
    assert out["body_pages"] == ["https://news.test/real"]
    pages = out["fill_evidence"]["attempts"][0]["pages"]
    assert [p["url"] for p in pages] == [
        "https://news.test/shell", "https://news.test/real"]
    assert pages[0]["canonical_follow"] == "https://news.test/real"
    assert pages[1]["canonical_follow"] is None


def test_canonical_loop_does_not_hang():
    loop_html = ('<html><head>'
                 '<link rel="canonical" href="https://news.test/loop-b">'
                 "</head><body></body></html>")
    loop_b = ('<html><head>'
              '<link rel="canonical" href="https://news.test/loop-a">'
              "</head><body></body></html>")
    _, con = _new_db()
    out = page_fill.process(
        _lead("https://news.test/loop-a", id="lp"),
        con=con, fetcher=MockFetcher({
            "https://news.test/loop-a": {"html": loop_html},
            "https://news.test/loop-b": {"html": loop_b},
        }), max_pages=5)
    assert out["fill_status"] == "shell"
    assert out["body"] is None
    visited = [p["url"]
               for p in out["fill_evidence"]["attempts"][0]["pages"]]
    assert visited == ["https://news.test/loop-a", "https://news.test/loop-b"]


def test_retryable_status_succeeds_within_attempts():
    url = "https://news.test/retry"
    fetcher = MockFetcher({url: [
        {"status": 503, "html": "<html></html>"},
        {"html": _page(["重试成功后的完整正文页面，段落字符足够长，判定完整。"])},
    ]})
    _, con = _new_db()
    out = page_fill.process(_lead(url, id="rt"), con=con,
                            fetcher=fetcher, max_attempts=2)
    assert out["fill_status"] == "complete"
    assert out["fill_evidence"]["attempts"][0]["pages"][0]["status"] == 200
    assert fetcher.calls[url] == 2

    # 始终 503：重试耗尽后落 shell 失败状态，可日后重试
    fetcher2 = MockFetcher({
        "https://news.test/retry2": {"status": 503, "html": "<html></html>"}})
    out2 = page_fill.process(_lead("https://news.test/retry2", id="rt2"),
                             con=con, fetcher=fetcher2, max_attempts=2)
    assert out2["fill_status"] == "shell"
    assert out2["body"] is None


def test_redirect_circle_is_error_status():
    routes = {
        "https://news.test/c1": {"status": 302,
                                 "headers": {"Location": "https://news.test/c2"}},
        "https://news.test/c2": {"status": 302,
                                 "headers": {"Location": "https://news.test/c1"}},
    }
    _, con = _new_db()
    out = page_fill.process(
        _lead("https://news.test/c1", id="rc"),
        con=con, fetcher=MockFetcher(routes))
    assert out["fill_status"] == "error"
    assert "CircularRedirect" in out["fill_reason"]


def test_mid_pagination_failure_is_idempotent_across_reruns():
    routes = {
        "https://news.test/pp": {"html": _page(
            ["第一页正文段落，内容足够长，构成文章开头的完整段落。"],
            next_url="https://news.test/pp/2")},
        "https://news.test/pp/2": {"error": "ConnectionResetError"},
    }
    _, con = _new_db()
    rec = _lead("https://news.test/pp", id="pp")
    r1 = page_fill.process(rec, con=con, fetcher=MockFetcher(routes))
    r2 = page_fill.process(rec, con=con, fetcher=MockFetcher(routes))
    r3 = page_fill.process(rec, con=con, fetcher=MockFetcher(routes))
    assert r1["fill_status"] == "error"
    state = _state_of(con, "pp")
    # 第一页内容未变 → 三次只留一次尝试，已拿到的第一页正文仍保留
    assert len(state["evidence"]["attempts"]) == 1
    assert r1["body"] is not None and "第一页" in r1["body"]
    assert r2["fill_evidence"] == r1["fill_evidence"]
    assert r3["body"] == r1["body"]

    # 第一页更新（内容指纹变化）后即使第二页仍失败，也应追加新尝试
    routes["https://news.test/pp"] = {"html": _page(
        ["第一页正文更新后的段落，内容同样足够长，构成新的文章开头。"],
        next_url="https://news.test/pp/2")}
    r4 = page_fill.process(rec, con=con, fetcher=MockFetcher(routes))
    assert r4["fill_status"] == "error"
    assert len(_state_of(con, "pp")["evidence"]["attempts"]) == 2
    assert "更新后" in r4["body"]


def test_redirect_chain_preserved_when_retry_ends_in_failure():
    routes = {
        "https://news.test/gone": {"status": 301, "headers": {
            "Location": "https://news.test/gone-2"}},
        "https://news.test/gone-2": {"error": "ConnectionRefusedError"},
    }
    _, con = _new_db()
    out = page_fill.process(
        _lead("https://news.test/gone", id="rf"),
        con=con, fetcher=MockFetcher(routes), max_attempts=3)
    assert out["fill_status"] == "error"
    chain = out["fill_evidence"]["attempts"][0]["redirect_chain"]
    assert [(h["from"], h["to"]) for h in chain] == [
        ("https://news.test/gone", "https://news.test/gone-2")]


def test_lead_change_triggers_refill_but_keeps_history():
    url = "https://news.test/moving"
    fetcher = MockFetcher({
        url + "/v1": {"html": _page(["线索 v1 指向的正文页面，段落足够长，判定完整。"])},
        url + "/v2": {"html": _page(["线索 v2 更新后的正文页面，段落同样足够长，判定完整。"])},
    })
    _, con = _new_db()
    rec1 = _lead(url + "/v1", id="mv")
    first = page_fill.process(rec1, con=con, fetcher=fetcher)
    assert first["fill_status"] == "complete"
    assert "v1" in first["body"]

    rec2 = _lead(url + "/v2", id="mv")  # 同一篇文章（id 不变），线索变更
    second = page_fill.process(rec2, con=con, fetcher=fetcher)
    state = _state_of(con, "mv")
    assert second["fill_status"] == "complete"
    assert "v2" in second["body"] and "v1" not in second["body"]
    assert len(state["evidence"]["attempts"]) == 2  # 旧尝试留证可回查
    assert state["evidence"]["attempts"][0]["lead_link"] == url + "/v1"
    assert state["evidence"]["attempts"][1]["lead_link"] == url + "/v2"

    # 再用 v2 跑：complete 状态直接回灌，零请求
    calls = dict(fetcher.calls)
    third = page_fill.process(rec2, con=con, fetcher=fetcher)
    assert fetcher.calls == calls
    assert third["body"] == second["body"]


# ---------------------------------------------------------------- 10. 无线索透传与流水线 happy-path

def test_record_without_lead_passes_through():
    rec = {"id": "plain-1", "src": "alpha", "name": "无 RSS 线索"}
    out = page_fill.process(rec)
    assert out["id"] == "plain-1"
    assert out["_stage_page_fill"] == "no_lead"
    assert "body" not in out


def test_pipeline_happy_path_remains_idempotent():
    db = os.path.join(tempfile.mkdtemp(prefix="pipe_"), "wire.db")
    from src.newswire import config
    old_cfg, old_store = config.DB_PATH, store.DB_PATH
    config.DB_PATH = db
    store.DB_PATH = db  # store 在导入时绑定了路径名，需同步切换
    try:
        a = pipeline.run()
        b = pipeline.run()
        assert a["fed"] == 4
        assert b["rows"] == a["rows"] == 3
        con = sqlite3.connect(db)
        try:
            keys = [k for k, _ in store.all_records(con)]
            assert len(keys) == len(set(keys))  # 无重复文章
        finally:
            con.close()
    finally:
        config.DB_PATH = old_cfg
        store.DB_PATH = old_store
