"""identity 归并：证据归并、保守分离、可拆回、顺序无关、幂等、兼容。"""
import json
from pathlib import Path

import pytest

from src.newswire import (dedup, identity, keyer, loader, normalizer,
                          pipeline, publish_time, rss_pull, store)

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "articles.json"
ARTICLES = json.loads(FIX.read_text(encoding="utf-8"))
BY_ID = {r["id"]: r for r in ARTICLES}

A_ID = identity._article_id("can:https://news.example.com/story/1001")
B_ID = identity._article_id("url:https://other.example.com/news/5678")
C_ID = identity._article_id("url:https://w1.example.com/a")
D1_ID = identity._article_id("url:https://w3.example.com/x")
D2_ID = identity._article_id("url:https://w4.example.com/y")


def flat_rules(rec):
    """展开 identity.evidence 中的规则名（含 scored 子信号）。"""
    rules = []
    for e in rec["identity"]["evidence"]:
        if "signals" in e:
            rules += [s["rule"] for s in e["signals"]]
        else:
            rules.append(e["rule"])
    return rules


def run_with(records, db_path, monkeypatch, reset=True):
    """用指定记录与库文件跑一遍流水线，返回 (counts, rows)。"""
    monkeypatch.setattr(store, "DB_PATH", db_path)
    monkeypatch.setattr(loader, "load", lambda: [dict(r) for r in records])
    if reset:
        identity.reset()
    con = store.connect()
    try:
        counts = pipeline.run(con)
        rows = store.all_records(con)
    finally:
        con.close()
    return counts, rows


# ---------- 基础规范化 ----------

def test_normalize_url_strips_tracking_and_fragment():
    u = identity.normalize_url(
        "HTTPS://News.Example.com/Story/1001/?utm_source=rss&b=2&a=1#frag")
    assert u == "https://news.example.com/Story/1001?a=1&b=2"


def test_normalize_url_query_order_independent():
    x = identity.normalize_url("https://e.com/p?a=1&b=2")
    y = identity.normalize_url("https://e.com/p?b=2&a=1")
    assert x == y


def test_title_fingerprint_absorbs_punctuation_and_case():
    assert identity.title_fingerprint("Central Bank Holds Rates Steady.") == \
        identity.title_fingerprint("central bank holds rates steady")


# ---------- 归并：canonical / 重定向 / 转载 / 多语言 / 内容更新 ----------

def test_merge_canonical_redirect_repost_multilingual_update():
    r = identity.IdentityResolver()
    out = {i: r.resolve(BY_ID[i]) for i in ["a-1", "a-2", "a-3", "a-4", "a-5"]}
    # 最终归属：全部指向同一文章对象（a-1 解析时的旧主键已被换键取代）
    assert r.assignment() == {i: A_ID for i in
                              ["a-1", "a-2", "a-3", "a-4", "a-5"]}
    # 各类线索都留下了证据
    assert "strong_key" in flat_rules(out["a-2"])           # canonical/URL
    assert "strong_key" in flat_rules(out["a-3"])           # 重定向 final_url
    assert "strong_key" in flat_rules(out["a-4"])           # translation_of 多语言
    assert "strong_key" in flat_rules(out["a-5"])           # reposted_from 转载
    # 别名集覆盖 canonical、原始 URL、AMP、多语言路径与转载镜像
    aliases = set(out["a-5"]["aliases"])
    assert "can:https://news.example.com/story/1001" in aliases
    assert "url:https://amp.news.example.com/story/1001" in aliases
    assert "url:https://news.example.com/story/1001/zh" in aliases
    assert "url:https://mirror.example.net/item/xyz" in aliases
    # 内容更新（a-2 updated 更晚）与多语言稿件都在历史来源里
    prov = {m["id"]: m for m in out["a-5"]["identity"]["provenance"]}
    assert set(prov) == {"a-1", "a-2", "a-3", "a-4", "a-5"}
    assert prov["a-2"]["updated"] == "2026-11-10T09:30:00Z"
    assert prov["a-4"]["lang"] == "zh"


# ---------- 保守分离：相似但独立 ----------

def test_similar_but_independent_not_merged():
    r = identity.IdentityResolver()
    for i in ["a-1", "a-2", "a-3", "a-4", "a-5"]:
        r.resolve(BY_ID[i])
    b = r.resolve(BY_ID["b-1"])   # 标题完全相同，但无任何其他证据
    assert b["article_id"] == B_ID != A_ID
    rej = b["identity"]["rejected"]
    assert rej and rej[0]["cluster"] == A_ID
    assert rej[0]["score"] == pytest.approx(0.5)
    assert rej[0]["threshold"] == identity.MERGE_THRESHOLD
    assert any(s["rule"] == "title_exact" for s in rej[0]["signals"])


def test_attachment_alone_does_not_merge():
    r = identity.IdentityResolver()
    d1 = r.resolve(BY_ID["d-1"])
    d2 = r.resolve(BY_ID["d-2"])  # 共享同一张通稿图片，标题完全不同
    assert d1["article_id"] == D1_ID
    assert d2["article_id"] == D2_ID
    rej = d2["identity"]["rejected"]
    assert rej and rej[0]["score"] == pytest.approx(0.6)
    assert any(s["rule"] == "attachment" for s in rej[0]["signals"])


def test_title_alone_does_not_merge():
    r = identity.IdentityResolver()
    x = r.resolve({"id": "x1", "url": "https://s1.example.com/a",
                   "title": "Storm Hits Coastal Town", "src": "s1"})
    y = r.resolve({"id": "x2", "url": "https://s2.example.com/b",
                   "title": "Storm Hits Coastal Town", "src": "s2"})
    assert x["article_id"] != y["article_id"]


# ---------- 归并：附件 + 标题 ----------

def test_attachment_plus_title_merges():
    r = identity.IdentityResolver()
    c1 = r.resolve(BY_ID["c-1"])
    c2 = r.resolve(BY_ID["c-2"])  # 附件带跟踪参数，标题多一个感叹号
    assert c1["article_id"] == c2["article_id"] == C_ID
    rules = flat_rules(c2)
    assert "attachment" in rules and "title_exact" in rules


def test_attachment_plus_fuzzy_title_merges():
    r = identity.IdentityResolver()
    x = r.resolve({"id": "x1", "url": "https://s1.example.com/a",
                   "title": "City Council Approves Transit Budget",
                   "attachments": ["https://cdn.example.com/d.pdf"], "src": "s1"})
    y = r.resolve({"id": "x2", "url": "https://s2.example.com/b",
                   "title": "City Council Approves New Transit Budget",
                   "attachments": ["https://cdn.example.com/d.pdf"], "src": "s2"})
    assert x["article_id"] == y["article_id"]
    rules = flat_rules(y)
    assert "attachment" in rules and "title_fuzzy" in rules


# ---------- 顺序无关 / 重复输入 ----------

def test_source_reorder_same_assignments():
    forward, backward = identity.IdentityResolver(), identity.IdentityResolver()
    for rec in ARTICLES:
        forward.resolve(dict(rec))
    for rec in reversed(ARTICLES):
        backward.resolve(dict(rec))
    # 最终归属与簇别名集与到达顺序无关
    assert forward.assignment() == backward.assignment()
    assert {tuple(sorted(c["aliases"])) for c in forward.clusters.values()} == \
           {tuple(sorted(c["aliases"])) for c in backward.clusters.values()}


def test_duplicate_input_idempotent():
    r = identity.IdentityResolver()
    first = r.resolve(BY_ID["c-1"])
    again = r.resolve(dict(BY_ID["c-1"]))
    assert again["article_id"] == first["article_id"] == C_ID
    assert again["identity"]["duplicate"] is True
    assert "duplicate_input" in flat_rules(again)
    assert len(r.clusters[C_ID]["members"]) == 1   # 成员不重复登记


# ---------- 拆回 ----------

def test_split_restores_keys_and_keeps_provenance():
    r = identity.IdentityResolver()
    r.resolve(BY_ID["c-1"])
    merged = r.resolve(BY_ID["c-2"])["article_id"]
    solo_c2 = identity.IdentityResolver().resolve(BY_ID["c-2"])["article_id"]

    plan = r.split(merged, ["c-2"], reason="wrong_merge")
    assert plan["new_article_id"] == solo_c2          # 恢复未归并时的主键
    assert plan["remaining_article_id"] == merged     # c-1 的 canonical 未变

    new_cl = r.clusters[plan["new_article_id"]]
    old_cl = r.clusters[plan["remaining_article_id"]]
    # 历史来源双方保留：归并与拆分事件均可追溯
    assert any(h["event"] == "merged" for h in new_cl["history"])
    assert any(h["event"] == "split_from" for h in new_cl["history"])
    assert any(h["event"] == "split_out" for h in old_cl["history"])
    assert [m["id"] for m in new_cl["members"]] == ["c-2"]
    assert [m["id"] for m in old_cl["members"]] == ["c-1"]
    # 拆回后再解析不会重新自动归并
    again = r.resolve(dict(BY_ID["c-2"]))
    assert again["article_id"] == plan["new_article_id"]
    assert any(j.get("reason") == "severed_by_manual_split"
               for j in again["identity"]["rejected"])


def test_split_rejects_unknown_target():
    r = identity.IdentityResolver()
    r.resolve(BY_ID["c-1"])
    with pytest.raises(ValueError):
        r.split("art_nonexistent", ["c-1"])
    with pytest.raises(ValueError):
        r.split(C_ID, ["nobody"])


def test_rebuild_from_store_allows_split(tmp_path, monkeypatch):
    counts, rows = run_with(ARTICLES, tmp_path / "r.db", monkeypatch)
    assert counts["rows"] == 5
    r2 = identity.IdentityResolver().rebuild([data for _, data in rows])
    assert C_ID in r2.clusters
    plan = r2.split(C_ID, ["c-2"])
    # 拆分后重新解析两条记录并落库：主键恢复、历史来源保留
    rec1 = r2.resolve(dict(BY_ID["c-1"]))
    rec2 = r2.resolve(dict(BY_ID["c-2"]))
    assert rec1["article_id"] == C_ID
    assert rec2["article_id"] == plan["new_article_id"]
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "r.db")
    con = store.connect()
    try:
        store.upsert(con, pipeline.Record(key=keyer.key_of(rec1), data=rec1))
        store.upsert(con, pipeline.Record(key=keyer.key_of(rec2), data=rec2))
        store.commit(con)
        keys = {k for k, _ in store.all_records(con)}
    finally:
        con.close()
    assert keys == {A_ID, B_ID, C_ID, D1_ID, D2_ID, plan["new_article_id"]}
    hist = [h["event"] for h in rec2["identity"]["history"]]
    assert "merged" in hist and "split_from" in hist


# ---------- 流水线集成 ----------

def test_pipeline_articles_merge_and_idempotent(tmp_path, monkeypatch):
    counts, rows = run_with(ARTICLES, tmp_path / "a.db", monkeypatch)
    assert counts == {"fed": 10, "rows": 5}
    keys = {k for k, _ in rows}
    assert keys == {A_ID, B_ID, C_ID, D1_ID, D2_ID}
    data = {k: d for k, d in rows}
    # 文章身份与别名关系随记录到达存储
    assert "can:https://news.example.com/story/1001" in data[A_ID]["aliases"]
    prov = {m["id"] for m in data[A_ID]["identity"]["provenance"]}
    assert prov == {"a-1", "a-2", "a-3", "a-4", "a-5"}
    # 相似但独立的报道未被误合并，且保守分离可解释
    assert data[B_ID]["identity"]["rejected"]
    # 同进程重跑与全新重放（注册表重建）都不增行
    assert run_with(ARTICLES, tmp_path / "a.db", monkeypatch,
                    reset=False)[0]["rows"] == 5
    assert run_with(ARTICLES, tmp_path / "a.db", monkeypatch,
                    reset=True)[0]["rows"] == 5


def test_pipeline_source_reorder_same_keys(tmp_path, monkeypatch):
    _, rows1 = run_with(ARTICLES, tmp_path / "f.db", monkeypatch)
    _, rows2 = run_with(list(reversed(ARTICLES)), tmp_path / "b.db", monkeypatch)
    assert {k for k, _ in rows1} == {k for k, _ in rows2}
    aliases1 = {k: tuple(d["aliases"]) for k, d in rows1}
    aliases2 = {k: tuple(d["aliases"]) for k, d in rows2}
    assert aliases1 == aliases2


def test_pipeline_duplicate_input_no_extra_rows(tmp_path, monkeypatch):
    counts, rows = run_with(ARTICLES + ARTICLES, tmp_path / "d.db", monkeypatch)
    assert counts["fed"] == 20
    assert counts["rows"] == 5
    data = {k: d for k, d in rows}
    assert len(data[A_ID]["identity"]["provenance"]) == 5


# ---------- 下游传递与兼容 ----------

def test_identity_flows_to_publish_time_and_dedup():
    identity.reset()
    rec = identity.process(dict(BY_ID["c-1"]))
    rec = publish_time.process(rec)
    rec = dedup.process(rec)
    assert rec["article_id"] == C_ID and rec["aliases"]
    assert rec["_stage_publish_time"] and rec["_stage_dedup"] == "ok"
    dup = dedup.process(identity.process(dict(BY_ID["c-1"])))
    assert dup["identity"]["duplicate"] is True
    assert dup["dup_of"] == C_ID
    identity.reset()


def test_record_without_signals_keeps_existing_identity():
    rec = {"id": "r-001", "src": "Alpha", "name": " Record One ",
           "value": "10", "status": "active", "ts": "2026-11-01T00:00:00Z"}
    for fn in (normalizer.process, rss_pull.process, identity.process,
               publish_time.process, dedup.process):
        rec = fn(rec)
    assert "article_id" not in rec
    assert rec["_stage_identity"] == "no_signals"
    assert keyer.key_of(rec) == "r-001"
