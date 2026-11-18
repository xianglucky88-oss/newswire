"""流水线集成测试：幂等、排序、迟到补全、崩溃恢复、旧记录兼容。"""
import json

import pytest

from src.newswire import identity, pipeline, publish_time, store
from src.newswire.models import Record


@pytest.fixture()
def con(tmp_path):
    c = store.connect(tmp_path / "test.db")
    yield c
    c.close()


def _published_map(c):
    return {k: (d.get("published_at"), d.get("publish_time", {}).get("revisions"))
            for k, d in store.all_records(c)}


def test_pipeline_runs_and_idempotent(con):
    a = pipeline.run(con)
    before = _published_map(con)
    b = pipeline.run(con)   # 重跑不增行、日期不变（幂等 upsert）
    assert a["fed"] == 10
    assert b["rows"] == a["rows"] == 9   # a-007-refetch 与 a-001 同文被去重
    assert _published_map(con) == before


def test_feed_sort_order(con):
    pipeline.run(con)
    rows = {d["id"]: d for _, d in store.all_records(con)}
    ordered_ids = [d["id"] for d in publish_time.sort_feed(rows.values())]
    # 有发布时间的按发布时间倒序；仅有修订时间的 a-006（11-16 修订）不得插队；
    # 完全无法还原的 a-004 垫底，且不得拿处理当天充数
    assert ordered_ids == ["a-008", "a-003", "a-009", "a-002", "a-001",
                           "a-005", "legacy-1", "a-006", "a-004"]
    assert rows["a-004"]["published_at"] is None
    assert rows["a-005"]["published_at"] == "2026-10-01T08:00:00+00:00"
    assert rows["a-005"]["revised_at"] == "2026-11-17T10:00:00+00:00"
    # 跨日时区：RSS 当地 11-01 深夜 = UTC 11-02
    assert rows["a-001"]["published_at"] == "2026-11-02T04:30:00+00:00"
    # 日期冲突：采用页面 meta 的 11-03，RSS 的 11-05 只作修订时间
    assert rows["a-002"]["published_at"] == "2026-11-03T09:00:00+00:00"
    assert rows["a-002"]["publish_time"]["basis_code"] == "adopted-with-conflict"


def test_late_fill_upgrades_with_revision_trail(con, tmp_path, monkeypatch):
    pipeline.run(con)
    key = next(k for k, d in store.all_records(con) if d["id"] == "a-008")
    first = store.get(con, key)
    assert first["published_at"] == "2026-11-11T22:30:00+00:00"  # 仅 RSS 线索
    assert first["publish_time"]["source"] == "rss"

    # 迟到的页面补全：页面在第二轮才可用
    pages = json.loads(open("fixtures/pages.json", encoding="utf-8").read())
    pages["https://gnn.example/news/late-fill"] = {
        "meta": {"published": "2026-11-11T22:00:00+00:00"}}
    late = tmp_path / "pages_late.json"
    late.write_text(json.dumps(pages), encoding="utf-8")
    monkeypatch.setattr("src.newswire.config.PAGES", late)

    pipeline.run(con)
    upgraded = store.get(con, key)
    assert upgraded["published_at"] == "2026-11-11T22:00:00+00:00"
    assert upgraded["publish_time"]["source"] == "page_meta"
    assert upgraded["publish_time"]["revisions"] == [{
        "from": "2026-11-11T22:30:00+00:00",
        "to": "2026-11-11T22:00:00+00:00",
        "source": "page_meta",
        "reason": "merge: adopted page_meta (adopted)",
    }]

    pipeline.run(con)  # 第三轮：稳定，不再追加修订
    third = store.get(con, key)
    assert third["published_at"] == "2026-11-11T22:00:00+00:00"
    assert len(third["publish_time"]["revisions"]) == 1


def test_crash_recovery_keeps_dates_stable(con, tmp_path, monkeypatch):
    clean = store.connect(tmp_path / "clean.db")
    pipeline.run(clean)
    expected = _published_map(clean)
    clean.close()

    calls = {"n": 0}
    real_commit = store.commit

    def flaky_commit(c):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash before commit")
        return real_commit(c)

    monkeypatch.setattr("src.newswire.store.commit", flaky_commit)
    with pytest.raises(RuntimeError):
        pipeline.run(con)
    con.rollback()  # 崩溃：未提交，恢复后重跑
    monkeypatch.setattr("src.newswire.store.commit", real_commit)
    pipeline.run(con)
    assert _published_map(con) == expected


def test_seeded_old_record_is_upgraded_on_merge(con):
    raw = {"id": "a-005", "src": "gnn",
           "url": "https://gnn.example/news/old-profile"}
    key = identity.process(raw)["article_key"]
    # 旧库里的记录：裸 ts 字段、没有 publish_time 块
    store.upsert(con, Record(key=key, data={
        "id": "a-005", "src": "gnn", "url": raw["url"],
        "ts": "2026-08-15T00:00:00Z"}))
    store.commit(con)

    pipeline.run(con)
    merged = store.get(con, key)
    # 页面 meta 的发布时间胜出；旧 ts 作为 legacy 候选保留，来源可查
    assert merged["published_at"] == "2026-10-01T08:00:00+00:00"
    sources = {c["source"] for c in merged["publish_time"]["candidates"]}
    assert "legacy" in sources and "page_meta" in sources


def test_store_roundtrip(con):
    assert store.count(con) >= 0
