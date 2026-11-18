"""文章重复/修订识别的端到端测试。

覆盖题目要求的场景：多平台转载、同文改版、撤稿后重发、独立新文、
更新时间反复变化、重入、部分写入恢复与旧批次回放。
"""
from __future__ import annotations

import json

import pytest

from src.newswire import dedup, identity, pipeline, store

BODY = (
    "The city opened a new bridge on Monday, officials said. "
    "The bridge connects the north bank with the old town. "
) * 3

REVISED_BODY = (
    "The city opened a new bridge on Monday, officials said. "
    "The bridge connects the north bank with the old town and will be free until 2027. "
) * 3

OTHER_BODY = (
    "The ferry operator said on Tuesday that two new routes will start next spring. "
    "Tickets will stay at the current price for the first year. "
) * 3


def rec(
    rid: str,
    source: str,
    url: str | None,
    title: str,
    body: str,
    published: str,
    *,
    updated: str | None = None,
    **extra,
):
    record = {
        "id": rid,
        "source": source,
        "title": title,
        "body": body,
        "published": published,
    }
    if url:
        record["url"] = url
    if updated:
        record["updated"] = updated
    record.update(extra)
    return record


def origin(updated: str | None = None):
    return rec(
        "a-1",
        "alpha",
        "https://alpha.example/news/bridge?utm_source=rss",
        "City opens new bridge",
        BODY,
        "2026-11-01T08:00:00Z",
        updated=updated,
    )


def syndicated(updated: str | None = None):
    return rec(
        "b-9",
        "beta",
        "https://www.beta.example/story/bridge?utm_medium=social",
        "City opens new bridge",
        BODY,
        "2026-11-01T12:00:00Z",
        updated=updated,
    )


def revision():
    return rec(
        "a-1",
        "alpha",
        "https://alpha.example/news/bridge",
        "City opens new bridge (updated)",
        REVISED_BODY,
        "2026-11-01T08:00:00Z",
        updated="2026-11-05T09:00:00Z",
    )


def retraction_notice(declared: str = "2026-11-06T00:00:00Z"):
    return {
        "id": "ret-1",
        "source": "alpha",
        "title": "[Retracted] City opens new bridge",
        "body": "This article has been retracted because a quote could not be verified.",
        "published": declared,
        "retracted": True,
        "retracts": "https://alpha.example/news/bridge",
        "reason": "unverified quote",
    }


def republication():
    return rec(
        "a-1",
        "alpha",
        "https://alpha.example/news/bridge",
        "City opens new bridge",
        BODY,
        "2026-11-07T08:00:00Z",
    )


def independent():
    return rec(
        "c-3",
        "gamma",
        "https://gamma.example/ferry",
        "Ferry service expands",
        OTHER_BODY,
        "2026-11-02T09:00:00Z",
    )


def far_future_reissue():
    return rec(
        "a-1b",
        "alpha",
        "https://alpha.example/news/bridge-2027",
        "City opens new bridge",
        BODY,
        "2027-06-01T08:00:00Z",
    )


def summary_only():
    return {
        "id": "s-1",
        "source": "alpha",
        "title": "City opens new bridge",
        "published": "2026-11-08T08:00:00Z",
    }


@pytest.fixture()
def con(tmp_path):
    connection = store.connect(tmp_path / "newswire.db")
    yield connection
    connection.close()


def run(con, records):
    return pipeline.run(con, records=records)


def articles(con):
    return {key: doc for key, doc in store.all_records(con)}


def only_article(con):
    docs = articles(con)
    assert len(docs) == 1, json.dumps(docs, ensure_ascii=False, indent=2)[:2000]
    return next(iter(docs.values()))


def first_article(con):
    return next(iter(articles(con).values()))


def test_syndication_merges_sources_without_extra_version(con):
    run(con, [origin(), syndicated()])
    doc = only_article(con)
    assert set(doc["sources"]) == {"alpha", "beta"}
    assert doc["version_count"] == 1  # 转载不是改版
    assert doc["event_count"] == 1
    assert doc["events"][0]["type"] == "syndication"
    assert doc["events"][0]["source"] == "beta"


def test_alternating_media_updated_timestamps_do_not_spawn_articles(con):
    run(con, [origin(), syndicated()])
    for stamp in (
        "2026-11-02T08:00:00Z",
        "2026-11-03T08:00:00Z",
        "2026-11-02T20:00:00Z",
    ):
        run(con, [origin(updated=stamp), syndicated(updated=stamp)])
    assert store.count(con) == 1
    doc = only_article(con)
    # updated 变化不是内容改版：版本数保持不变，只留下观察证据。
    assert doc["version_count"] == 1
    assert {e["type"] for e in doc["events"]} <= {"syndication", "observed"}
    assert "observed" in {e["type"] for e in doc["events"]}
    assert store.version_count(con) == 1


def test_revision_adds_version_and_keeps_sources(con):
    run(con, [origin(), syndicated()])
    run(con, [revision()])
    doc = only_article(con)
    assert doc["version_count"] == 2
    assert [v["content_digest"] for v in doc["versions"]][0] != [
        v["content_digest"] for v in doc["versions"]
    ][1]
    assert "revision" in {e["type"] for e in doc["events"]}
    # 修订没有丢掉转载来源证据。
    assert set(doc["sources"]) == {"alpha", "beta"}
    assert [v["published_at"] for v in doc["versions"]][0] == "2026-11-01T08:00:00Z"


def test_retraction_and_republication_are_distinct_relations(con):
    run(con, [origin(), syndicated(), revision()])
    run(con, [retraction_notice()])
    doc = only_article(con)
    assert doc["status"] == "retracted"
    assert store.event_count(con, type_="retraction") == 1

    run(con, [republication()])
    doc = only_article(con)
    assert doc["status"] == "active"
    types = [e["type"] for e in doc["events"]]
    assert "retraction" in types and "republication" in types
    # 重发使用旧内容：不产生重复版本。
    assert doc["version_count"] == 2
    # 撤稿证据没有被重发抹掉。
    assert store.event_count(con, type_="retraction") == 1


def test_independent_article_is_not_absorbed(con):
    run(con, [origin(), syndicated(), independent()])
    assert store.count(con) == 2


def test_identical_content_far_apart_is_a_new_article(con):
    run(con, [origin()])
    run(con, [far_future_reissue()])
    assert store.count(con) == 2
    reissued = next(doc for doc in articles(con).values() if doc["first_published_at"].startswith("2027"))
    assert reissued["events"][0]["type"] == "republication"
    assert reissued["events"][0]["kind"] == "independent_reissue"
    assert reissued["events"][0]["target"]  # 指向旧文，证据未丢


def test_summary_only_copy_is_not_an_extra_version_or_article(con):
    run(con, [origin()])
    run(con, [summary_only()])
    doc = only_article(con)
    assert doc["version_count"] == 1
    assert doc["event_count"] == 1
    assert doc["events"][0]["type"] == "syndication"


def test_replaying_old_batches_does_not_grow_versions_or_resurrect(con):
    batches = [
        [origin(), syndicated()],
        [revision()],
        [retraction_notice()],
        [republication()],
        [independent()],
    ]
    for batch in batches:
        run(con, batch)
    before_rows = store.count(con)
    before_versions = store.version_count(con)
    before = {(k, d["status"], d["version_count"]) for k, d in articles(con).items()}

    for batch in batches:  # 完整旧批次回放
        run(con, batch)

    after = {(k, d["status"], d["version_count"]) for k, d in articles(con).items()}
    assert store.count(con) == before_rows
    assert store.version_count(con) == before_versions
    assert after == before
    # 回放撤稿前的记录不能把文章从 retracted 复活后再次误判。
    doc = next(d for d in articles(con).values() if d["version_count"] == 2)
    assert doc["status"] == "active"


def test_partial_write_recovery_converges_to_full_run(tmp_path, monkeypatch):
    records = [
        origin(),
        syndicated(),
        revision(),
        retraction_notice(),
        republication(),
        independent(),
        far_future_reissue(),
    ]

    clean = store.connect(tmp_path / "clean.db")
    try:
        pipeline.run(clean, records=records)
        expected_rows = store.count(clean)
        expected_versions = {
            key: doc["version_count"] for key, doc in store.all_records(clean)
        }
        expected_status = {key: doc["status"] for key, doc in store.all_records(clean)}
        expected_evidence = {
            key: sorted({e["type"] for e in doc["events"] if e["type"] != "observed"})
            for key, doc in store.all_records(clean)
        }
    finally:
        clean.close()

    broken = store.connect(tmp_path / "broken.db")
    real_upsert = store.upsert
    calls = {"n": 0}

    def flaky_upsert(con, record):
        calls["n"] += 1
        if calls["n"] == 4:
            raise RuntimeError("simulated crash mid-batch")
        return real_upsert(con, record)

    monkeypatch.setattr(store, "upsert", flaky_upsert)
    with pytest.raises(RuntimeError):
        pipeline.run(broken, records=records)
    wrote_before_crash = store.count(broken)
    assert 0 < wrote_before_crash < len(records)
    monkeypatch.setattr(store, "upsert", real_upsert)

    # 重入：继续跑完整批次，索引从已提交的文章重建。
    pipeline.run(broken, records=records)
    assert store.count(broken) == expected_rows
    assert {key: doc["version_count"] for key, doc in store.all_records(broken)} == expected_versions
    assert {key: doc["status"] for key, doc in store.all_records(broken)} == expected_status
    # 关系证据必须收敛；额外的观察（observed）只说明同一条记录被重跑过一次，
    # 不是凭空新增的关系或文章。
    assert {
        key: sorted({e["type"] for e in doc["events"] if e["type"] != "observed"})
        for key, doc in store.all_records(broken)
    } == expected_evidence
    broken.close()


def test_version_chain_is_persisted_immutably(con):
    run(con, [origin(), syndicated(), revision()])
    versions = store.all_versions(con)
    assert len(versions) == 2
    # 版本键不可变：重复写入不会新增。
    run(con, [revision(), origin(), syndicated()])
    assert len(store.all_versions(con)) == 2


def test_url_canonicalization_merges_tracking_and_amp_variants():
    variants = [
        "https://www.alpha.example/news/bridge?utm_source=rss&utm_medium=social",
        "http://alpha.example/news/bridge/",
        "https://m.alpha.example/news/bridge/amp",
        "https://alpha.example/news/bridge#comments",
    ]
    assert len({identity.canonicalize_url(v) for v in variants}) == 1


def test_alias_scoping_avoids_cross_source_collisions():
    a = identity.identity_of({"source": "alpha", "id": "1", "url": "https://alpha.example/a"})
    b = identity.identity_of({"source": "beta", "id": "1", "url": "https://beta.example/b"})
    assert "1" not in a["aliases"]
    assert "alpha:1" in a["aliases"]
    assert "beta:1" in b["aliases"]


def test_source_id_alias_is_not_treated_as_url():
    assert identity.canonicalize_url("alpha:h1") == "alpha:h1"
    assert identity.looks_like_url("alpha:h1") is False
    assert identity.looks_like_url("https://alpha.example/a") is True
    assert identity.looks_like_url("alpha.example/a") is True


def test_retraction_can_target_a_source_scoped_alias(con):
    run(
        con,
        [
            rec(
                "h1",
                "alpha",
                "https://alpha.example/harbour",
                "Harbour project approved",
                BODY,
                "2026-11-01T08:00:00Z",
            )
        ],
    )
    run(
        con,
        [
            {
                "id": "r-9",
                "source": "alpha",
                "title": "[Retracted] Harbour project approved",
                "body": "The article has been retracted.",
                "published": "2026-11-05T08:00:00Z",
                "retracts": "alpha:h1",
            }
        ],
    )
    doc = only_article(con)
    assert doc["status"] == "retracted"
    assert store.event_count(con, type_="retraction") == 1


def test_correction_is_a_relation_not_a_new_article(con):
    run(con, [origin()])
    correction = {
        "id": "corr-1",
        "source": "alpha",
        "title": "Correction: City opens new bridge",
        "body": "An earlier version misstated the opening date.",
        "published": "2026-11-04T08:00:00Z",
        "correction_of": "https://alpha.example/news/bridge",
    }
    run(con, [correction])
    doc = only_article(con)
    assert doc["status"] == "corrected"
    assert store.event_count(con, type_="correction") == 1
    # 更正声明（指挥链证据）不覆盖原文内容版本。
    assert doc["version_count"] == 1


def test_same_title_with_different_full_bodies_stays_separate(con):
    run(
        con,
        [
            rec(
                "d-1",
                "alpha",
                "https://alpha.example/briefing-1",
                "Daily briefing",
                ("Monday: council approves budget. " * 20),
                "2026-11-01T08:00:00Z",
            ),
            rec(
                "d-2",
                "beta",
                "https://beta.example/briefing-2",
                "Daily briefing",
                ("Tuesday: ferry strike ends after talks. " * 20),
                "2026-11-02T08:00:00Z",
            ),
        ],
    )
    assert store.count(con) == 2


def test_same_title_no_body_does_not_merge_distinct_articles(con):
    run(
        con,
        [
            rec("e-1", "alpha", "https://alpha.example/1", "Morning brief", "", "2026-11-01T08:00:00Z"),
            rec("e-2", "beta", "https://beta.example/2", "Morning brief", "", "2026-11-02T08:00:00Z"),
        ],
    )
    assert store.count(con) == 2


def test_retraction_without_target_keeps_its_own_history(con):
    notice = {
        "id": "ret-2",
        "source": "delta",
        "title": "Retracted: Mystery report",
        "body": "The report has been retracted.",
        "published": "2026-11-09T00:00:00Z",
    }
    run(con, [notice])
    assert store.count(con) == 1
    doc = only_article(con)
    assert doc["status"] == "retracted"
    assert store.event_count(con, type_="retraction") == 1
    # 再次声明同一条撤稿不新增文章或事件。
    run(con, [notice])
    assert store.count(con) == 1
    assert store.event_count(con, type_="retraction") == 1


def test_same_url_edit_keeps_article_key_stable(con):
    run(con, [origin()])
    key_before = list(articles(con))[0]
    run(con, [revision(), origin(updated="2026-12-01T00:00:00Z")])
    assert list(articles(con)) == [key_before]


def test_joint_publishers_join_the_source_set(con):
    article = rec(
        "j-1",
        "alpha",
        "https://alpha.example/joint",
        "Joint probe into harbour contract",
        BODY,
        "2026-11-01T08:00:00Z",
        co_sources=["beta", "gamma"],
    )
    run(con, [article])
    doc = only_article(con)
    assert set(doc["sources"]) == {"alpha", "beta", "gamma"}
    assert doc["sources"]["alpha"]["roles"] == ["origin"]
    assert "co_publisher" in doc["sources"]["beta"]["roles"]
    assert "co_publisher" in doc["sources"]["gamma"]["roles"]


def test_dedup_process_is_pure(con):
    record = identity.process(origin())
    record = __import__("src.newswire.publish_time", fromlist=["process"]).process(record)
    record = __import__("src.newswire.retract", fromlist=["process"]).process(record)
    snapshot = json.dumps(record, sort_keys=True, ensure_ascii=False)
    dedup.process(record)
    assert json.dumps(record, sort_keys=True, ensure_ascii=False) == snapshot
