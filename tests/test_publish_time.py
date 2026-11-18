"""publish_time 单元测试：候选、采用、置信依据、冲突、时区、相对时间、合并与排序。"""
from src.newswire import publish_time as pt


def _rec_with_clues(clues, **extra):
    rec = {"time_clues": clues}
    rec.update(extra)
    return pt.process(rec)


# ---- 三类时间值的区分 ----

def test_absolute_relative_unrecoverable_are_distinct():
    abs_rec = _rec_with_clues({"rss": {"published": "2026-11-05T09:00:00+00:00"}})
    rel_rec = _rec_with_clues(
        {"body": {"relative": "3 hours ago", "ref": "2026-11-10T12:00:00Z"}})
    none_rec = pt.process({})

    assert abs_rec["publish_time"]["kind"] == "absolute"
    assert abs_rec["published_at"] == "2026-11-05T09:00:00+00:00"
    assert rel_rec["publish_time"]["kind"] == "relative"
    assert rel_rec["publish_time"]["basis_code"] == "relative-approximate"
    assert none_rec["published_at"] is None
    assert none_rec["publish_time"]["basis_code"] == "unrecoverable"


def test_unrecoverable_never_falls_back_to_processing_day():
    rec = pt.process({"id": "x", "fetched_at": "2026-11-18T08:00:00Z"})
    assert rec["published_at"] is None
    assert "NOT used" in rec["publish_time"]["basis"]
    # 处理当天（2026-11-18）不得出现在任何输出里
    assert "2026-11-18" not in str(rec["publish_time"]["published_at"])


# ---- 跨日时区 ----

def test_cross_day_timezone_same_instants_are_not_conflict():
    rec = _rec_with_clues({
        "rss": {"published": "Sun, 01 Nov 2026 23:30:00 -0500"},  # 当地 11-01
        "page_meta": {"published": "2026-11-02T12:30:00+08:00"},  # 当地 11-02
    })
    block = rec["publish_time"]
    # 本地日期相差一天，但归一化到 UTC 后是同一瞬时 → 不算冲突
    assert rec["published_at"] == "2026-11-02T04:30:00+00:00"
    assert block["source"] == "page_meta"
    assert block["basis_code"] == "adopted"
    assert "agree" in block["basis"]
    values = {c["source"]: c["value"] for c in block["candidates"]}
    assert values["rss"] == values["page_meta"] == "2026-11-02T04:30:00+00:00"


def test_named_timezone_hint_and_naive_assumption():
    rec = _rec_with_clues(
        {"body": {"date": "November 9, 2026", "tz": "America/New_York"}})
    cand = rec["publish_time"]["candidates"][0]
    assert rec["published_at"] == "2026-11-09T05:00:00+00:00"  # EST = UTC-5
    assert cand["precision"] == "day"

    naive = _rec_with_clues({"rss": {"published": "2026-11-05T09:00:00"}})
    cand = naive["publish_time"]["candidates"][0]
    assert naive["published_at"] == "2026-11-05T09:00:00+00:00"
    assert "tz-assumed-utc" in cand["note"]
    assert cand["confidence"] < 0.85  # 裸时间打了时区假设折扣


# ---- 日期冲突 ----

def test_conflicting_dates_adopt_higher_confidence_with_basis():
    rec = _rec_with_clues({
        "rss": {"published": "2026-11-05T09:00:00+00:00"},
        "page_meta": {"published": "2026-11-03T09:00:00+00:00",
                      "modified": "2026-11-05T09:00:00+00:00"},
    })
    block = rec["publish_time"]
    assert rec["published_at"] == "2026-11-03T09:00:00+00:00"
    assert block["source"] == "page_meta"
    assert block["basis_code"] == "adopted-with-conflict"
    assert "conflict" in block["basis"] and "rss" in block["basis"]
    # 冲突双方都在候选里，来源可查
    sources = {c["source"] for c in block["candidates"]}
    assert {"rss", "page_meta"} <= sources
    # RSS 的 11-05 实为修订时间：进 revised_at，不进 published_at
    assert rec["revised_at"] == "2026-11-05T09:00:00+00:00"


# ---- 相对时间 ----

def test_relative_time_uses_fetch_reference_not_now():
    rec = _rec_with_clues(
        {"body": {"relative": "3 hours ago", "ref": "2026-11-10T12:00:00Z"}})
    assert rec["published_at"] == "2026-11-10T09:00:00+00:00"
    assert rec["publish_time"]["confidence"] < 0.7  # 相对时间打折


def test_relative_without_reference_is_unrecoverable():
    rec = _rec_with_clues({"body": {"relative": "3 hours ago"}})
    assert rec["published_at"] is None
    cand = rec["publish_time"]["candidates"][0]
    assert cand["value"] is None
    assert "relative-without-reference" in cand["note"]


# ---- 修订时间 ≠ 发布时间 ----

def test_revision_time_never_becomes_publish_time():
    rec = _rec_with_clues({"rss": {"updated": "2026-11-16T09:00:00+00:00"}})
    block = rec["publish_time"]
    assert rec["published_at"] is None
    assert rec["revised_at"] == "2026-11-16T09:00:00+00:00"
    assert block["basis_code"] == "revision-only"


def test_updated_old_article_does_not_jump_to_front():
    old_updated = pt.process({
        "article_key": "old", "time_clues": {
            "page_meta": {"published": "2026-10-01T08:00:00+00:00",
                          "modified": "2026-11-17T10:00:00+00:00"}}})
    fresh = pt.process({
        "article_key": "fresh", "time_clues": {
            "rss": {"published": "2026-11-11T22:30:00+00:00"}}})
    revision_only = pt.process({
        "article_key": "rev", "time_clues": {
            "rss": {"updated": "2026-11-18T09:00:00+00:00"}}})
    unknown = pt.process({"article_key": "unk"})

    ordered = [r["article_key"] for r in pt.sort_feed(
        [unknown, revision_only, old_updated, fresh])]
    # 10 月发布、11 月修订的旧文不得挤到 11-11 发布的新文前面；
    # 仅有 11-18 修订时间的记录排所有有发布时间的记录之后
    assert ordered == ["fresh", "old", "rev", "unk"]


def test_sort_key_stable_tiebreak_by_article_key():
    a = pt.process({"article_key": "a", "time_clues": {
        "rss": {"published": "2026-11-05T09:00:00+00:00"}}})
    b = pt.process({"article_key": "b", "time_clues": {
        "rss": {"published": "2026-11-05T09:00:00+00:00"}}})
    assert [r["article_key"] for r in pt.sort_feed([b, a])] == ["a", "b"]


# ---- 旧记录字段兼容 ----

def test_legacy_bare_fields_read_as_legacy_candidates():
    rec = pt.process({"id": "old", "ts": "2026-08-01T10:00:00Z",
                      "updated": "2026-09-01T00:00:00Z"})
    block = rec["publish_time"]
    assert rec["published_at"] == "2026-08-01T10:00:00+00:00"
    assert block["source"] == "legacy"
    assert rec["revised_at"] == "2026-09-01T00:00:00+00:00"
    notes = [c["note"] for c in block["candidates"]]
    assert any("legacy-field:ts" in n for n in notes)


def test_own_output_is_not_reused_as_input_on_reprocess():
    once = pt.process({"time_clues": {"rss": {"published": "2026-11-05T09:00:00+00:00"}}})
    twice = pt.process(once)
    assert twice["published_at"] == once["published_at"]
    assert len(twice["publish_time"]["candidates"]) == \
        len(once["publish_time"]["candidates"])


# ---- 合并：重复抓取 / 迟到补全 ----

def test_merge_late_fill_upgrades_and_leaves_revision_trail():
    rss_only = pt.process({"time_clues": {
        "rss": {"published": "2026-11-11T22:30:00+00:00"}}})
    with_page = pt.process({"time_clues": {
        "rss": {"published": "2026-11-11T22:30:00+00:00"},
        "page_meta": {"published": "2026-11-11T22:00:00+00:00"}}})
    merged = pt.merge_records(rss_only, with_page)
    block = merged["publish_time"]
    assert merged["published_at"] == "2026-11-11T22:00:00+00:00"
    assert block["revisions"] == [{
        "from": "2026-11-11T22:30:00+00:00",
        "to": "2026-11-11T22:00:00+00:00",
        "source": "page_meta",
        "reason": "merge: adopted page_meta (adopted)",
    }]


def test_merge_refetch_with_less_info_keeps_publish_time():
    full = pt.process({"time_clues": {
        "rss": {"published": "2026-11-11T22:30:00+00:00"},
        "page_meta": {"published": "2026-11-11T22:00:00+00:00"}}})
    refetch_rss_only = pt.process({"time_clues": {
        "rss": {"published": "2026-11-11T22:30:00+00:00"}}})
    merged = pt.merge_records(full, refetch_rss_only)
    assert merged["published_at"] == "2026-11-11T22:00:00+00:00"
    assert merged["publish_time"]["revisions"] == []  # 未变化不留痕


def test_merge_identical_refetch_is_stable():
    once = pt.process({"time_clues": {
        "rss": {"published": "2026-11-11T22:30:00+00:00"}}})
    again = pt.process({"time_clues": {
        "rss": {"published": "2026-11-11T22:30:00+00:00"}}})
    merged = pt.merge_records(once, again)
    assert merged["publish_time"] == once["publish_time"]


def test_unparseable_candidate_kept_for_provenance():
    rec = _rec_with_clues({"rss": {"published": "not-a-date"}})
    cand = rec["publish_time"]["candidates"][0]
    assert cand["value"] is None and cand["confidence"] == 0.0
    assert "unparseable" in cand["note"]
    assert rec["published_at"] is None
