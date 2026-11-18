"""单一职责：文章身份归并（identity）。

判定每条记录指向哪个文章对象，把文章身份（article_id）与别名关系
（aliases）注解到记录上，随记录传给 publish_time / dedup / store；
具体修订事件的判定仍由下游逻辑负责。

归并界线（可解释）：
- 强键命中即归并：规范化 URL（去跟踪参数、去 fragment、查询参数排序）、
  canonical_url、重定向 final_url/redirects_to、guid、
  显式关联（same_as / translation_of / reposted_from / origin_url）。
- 弱信号累计加分：共享附件 0.6、标题规范化一致 0.5、标题高度相似 0.4、
  同源 0.1；合计 >= MERGE_THRESHOLD(1.0) 才自动归并 —— 单一弱信号
  （哪怕标题完全相同）不足以归并，相似但独立的报道因此保持分离。
- 得分处于 [CONSIDER_FLOOR, MERGE_THRESHOLD) 的候选记入
  identity.rejected 并说明差距，保守分离的边界可审计。

每次归并都记录证据（identity.evidence）与历史来源（identity.provenance /
identity.history）；错误归并可用 IdentityResolver.split() 拆回，
拆分后各方恢复独立身份、历史来源不丢，拆分对（severed）不再自动归并。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MERGE_THRESHOLD = 1.0   # 自动归并所需最低累计分
CONSIDER_FLOOR = 0.5    # 达到即记录为“考虑过但证据不足”的候选
FUZZY_TITLE_MIN = 0.8   # 标题 token Jaccard 达到该值视为高度相似

W_ATTACHMENT = 0.6
W_TITLE_EXACT = 0.5
W_TITLE_FUZZY = 0.4
W_SAME_SOURCE = 0.1

TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "fbclid", "gclid", "dclid", "mc_cid", "mc_eid",
    "igshid", "spm", "ref", "ref_src", "_ga",
})

# 别名键种类优先级：canonical 显式声明优先，其次 URL，再次 guid，最后记录 id
_KIND_PRIORITY = {"can": 0, "url": 1, "guid": 2, "rid": 3}

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def normalize_url(u):
    """规范化 URL：小写 scheme/host、去跟踪参数、去 fragment、
    查询参数排序（来源换序不影响结果）、去尾部斜杠。"""
    s = str(u or "").strip()
    if not s:
        return ""
    sp = urlsplit(s)
    scheme = (sp.scheme or "http").lower()
    netloc = sp.netloc.lower()
    path = sp.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    qs = sorted(
        (k, v) for k, v in parse_qsl(sp.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    )
    return urlunsplit((scheme, netloc, path, urlencode(qs), ""))


def title_fingerprint(t):
    """标题指纹：NFKC、casefold、去标点、压缩空白（吸收轻微标题变化）。"""
    t = unicodedata.normalize("NFKC", str(t or "")).casefold()
    t = _PUNCT.sub(" ", t)
    return _WS.sub(" ", t).strip()


def title_similarity(fp_a, fp_b):
    """两个标题指纹的 token Jaccard 相似度。"""
    a, b = set(fp_a.split()), set(fp_b.split())
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _article_id(canonical_key):
    return "art_" + hashlib.sha256(canonical_key.encode("utf-8")).hexdigest()[:16]


def _key_sort(key):
    kind, _, _ = key.partition(":")
    return (_KIND_PRIORITY.get(kind, 9), key)


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return list(v)
    return [v]


def extract_signals(rec):
    """抽取身份线索；记录没有任何文章线索时返回 None（保持原有身份兼容）。"""
    keys, urls = set(), set()

    def add_url(kind, value):
        nu = normalize_url(value)
        if nu:
            urls.add(nu)
            keys.add(kind + ":" + nu)

    add_url("url", rec.get("url"))
    add_url("can", rec.get("canonical_url"))
    for f in ("final_url", "redirects_to"):
        for v in _as_list(rec.get(f)):
            add_url("url", v)
    for f in ("same_as", "translation_of", "reposted_from", "origin_url"):
        for v in _as_list(rec.get(f)):
            v = str(v).strip()
            if not v:
                continue
            if "://" in v:
                add_url("url", v)
            else:
                keys.add("rid:" + v)
    guid = str(rec.get("guid") or "").strip()
    if guid:
        keys.add("guid:" + guid)
    attachments = set()
    for v in _as_list(rec.get("attachments")) + _as_list(rec.get("enclosure")):
        if isinstance(v, dict):
            v = v.get("url", "")
        nu = normalize_url(v)
        if nu:
            attachments.add(nu)
    fp = title_fingerprint(rec.get("title"))
    if not (urls or guid or attachments or fp or any(k.startswith("rid:") for k in keys)):
        return None
    rid = str(rec.get("id") or "").strip()
    if rid:
        keys.add("rid:" + rid)
    return {
        "keys": keys,
        "attachments": attachments,
        "title_fp": fp,
        "src": str(rec.get("src") or "").strip().lower(),
        "lang": str(rec.get("lang") or "").strip().lower(),
    }


def _member_summary(rec, sig):
    """成员快照：保留拆回所需的全部线索与历史来源。"""
    return {
        "id": rec.get("id"),
        "src": sig["src"],
        "lang": sig["lang"],
        "url": str(rec.get("url") or ""),
        "title": str(rec.get("title") or ""),
        "ts": str(rec.get("ts") or rec.get("published") or ""),
        "updated": str(rec.get("updated") or ""),
        "keys": sorted(sig["keys"], key=_key_sort),
        "attachments": sorted(sig["attachments"]),
        "title_fp": sig["title_fp"],
    }


def _score_against(sig, cluster):
    """与簇内成员逐一对照，返回（最高累计分, 对应证据）。弱信号才计分。"""
    best_score, best_ev = 0.0, []
    for m in cluster["members"]:
        score, ev = 0.0, []
        shared = sorted(set(sig["attachments"]) & set(m["attachments"]))
        if shared:
            score += W_ATTACHMENT
            ev.append({"rule": "attachment", "detail": shared, "weight": W_ATTACHMENT})
        if sig["title_fp"] and m["title_fp"]:
            if sig["title_fp"] == m["title_fp"]:
                score += W_TITLE_EXACT
                ev.append({"rule": "title_exact", "detail": sig["title_fp"],
                           "weight": W_TITLE_EXACT})
            else:
                sim = title_similarity(sig["title_fp"], m["title_fp"])
                if sim >= FUZZY_TITLE_MIN:
                    score += W_TITLE_FUZZY
                    ev.append({"rule": "title_fuzzy", "detail": round(sim, 3),
                               "weight": W_TITLE_FUZZY})
        if sig["src"] and sig["src"] == m["src"]:
            score += W_SAME_SOURCE
            ev.append({"rule": "same_source", "detail": sig["src"],
                       "weight": W_SAME_SOURCE})
        if score > best_score:
            best_score, best_ev = score, ev
    return best_score, best_ev


class IdentityResolver:
    """增量身份归并器：强键直接归并，弱信号达阈值归并，全程留证可拆回。

    最终归并结果与记录到达顺序无关：簇身份 = 簇内最小别名键的哈希，
    归并只让集合变大、canonical 只沿优先级/字典序单调收敛。
    """

    def __init__(self, threshold=MERGE_THRESHOLD, floor=CONSIDER_FLOOR):
        self.threshold = threshold
        self.floor = floor
        self.clusters = {}        # article_id -> cluster
        self.key_to_article = {}  # 别名键 -> article_id
        self.severed = set()      # 人工拆分对 frozenset({id1, id2})，不再自动归并

    @staticmethod
    def _canonical(keys):
        return min(keys, key=_key_sort)

    def _register(self, cl):
        self.clusters[cl["article_id"]] = cl
        for k in cl["aliases"]:
            self.key_to_article[k] = cl["article_id"]

    def _create(self, keys, member, evidence):
        canonical = self._canonical(keys)
        cl = {
            "article_id": _article_id(canonical),
            "canonical_key": canonical,
            "aliases": set(keys),
            "members": [member],
            "history": [{"event": "created", "canonical_key": canonical,
                         "evidence": evidence}],
        }
        self._register(cl)
        return cl

    def _merge(self, art_ids, extra_keys, member, evidence):
        keys, members, history = set(extra_keys), [], []
        for aid in art_ids:
            cl = self.clusters.pop(aid)
            keys |= cl["aliases"]
            members += cl["members"]
            history += cl["history"]
        if member is not None:
            members.append(member)
        canonical = self._canonical(keys)
        new_id = _article_id(canonical)
        history.append({
            "event": "merged",
            "into": new_id,
            "from": sorted(art_ids),
            "canonical_key": canonical,
            "evidence": evidence,
        })
        cl = {"article_id": new_id, "canonical_key": canonical,
              "aliases": keys, "members": members, "history": history}
        self._register(cl)
        supersedes = [aid for aid in art_ids if aid != new_id]
        return cl, supersedes

    def resolve(self, record):
        """判定记录指向的文章对象，返回带身份注解的新记录。

        注解反映解析时刻的簇状态；若后续记录引发换键（canonical 收敛），
        被取代的旧主键通过记录的 supersedes 字段通知 store 修正，
        最终归属以 clusters / assignment() 为准（与到达顺序无关）。
        """
        rec = dict(record)
        sig = extract_signals(rec)
        if sig is None:
            rec["_stage_identity"] = "no_signals"
            return rec
        keys = sig["keys"]
        member = _member_summary(rec, sig)
        touched, seen = [], set()
        for k in keys:
            aid = self.key_to_article.get(k)
            if aid and aid not in seen:
                seen.add(aid)
                touched.append(aid)
        evidence, rejected, merge_arts = [], [], []
        duplicate = False
        for aid in touched:
            shared = sorted((k for k in keys if k in self.clusters[aid]["aliases"]),
                            key=_key_sort)
            evidence.append({"rule": "strong_key", "with": aid,
                             "keys": shared, "weight": 1.0})
            if member["id"] and any(m["id"] == member["id"]
                                    for m in self.clusters[aid]["members"]):
                duplicate = True
        for aid, cl in self.clusters.items():
            if aid in seen:
                continue
            if member["id"] and any(
                m["id"] and frozenset((member["id"], m["id"])) in self.severed
                for m in cl["members"]
            ):
                rejected.append({"cluster": aid, "reason": "severed_by_manual_split",
                                 "detail": "人工拆分对，禁止自动归并"})
                continue
            score, ev = _score_against(sig, cl)
            if score >= self.threshold:
                merge_arts.append(aid)
                evidence.append({"rule": "scored", "with": aid,
                                 "score": round(score, 3), "signals": ev})
            elif score >= self.floor:
                rejected.append({
                    "cluster": aid, "score": round(score, 3),
                    "threshold": self.threshold,
                    "reason": "证据不足：%.2f < %.2f，保持分离" % (score, self.threshold),
                    "signals": ev,
                })
        supersedes = []
        if not touched and not merge_arts:
            cl = self._create(keys, member, evidence)
        elif (duplicate and not merge_arts and len(touched) == 1
              and keys <= self.clusters[touched[0]]["aliases"]):
            # 重复输入：簇状态不变，只标注
            cl = self.clusters[touched[0]]
            evidence = [{"rule": "duplicate_input", "with": touched[0], "weight": 1.0}]
        else:
            cl, supersedes = self._merge(touched + merge_arts, keys,
                                         None if duplicate else member, evidence)
        rec["article_id"] = cl["article_id"]
        rec["aliases"] = sorted(cl["aliases"], key=_key_sort)
        rec["identity"] = {
            "evidence": evidence,
            "rejected": rejected,
            "duplicate": duplicate,
            "provenance": [dict(m) for m in cl["members"]],
            "history": [dict(h) for h in cl["history"]],
        }
        if supersedes:
            rec["supersedes"] = supersedes
        rec["_stage_identity"] = "ok"
        return rec

    def split(self, article_id, member_ids, reason="manual_split"):
        """把指定成员从簇中拆出，恢复独立身份；双方保留全部历史来源。

        返回拆分方案（新簇 id、留存簇 id、需要替换的主键 supersedes）。
        拆出的成员按自身线索重新计算身份，与未归并时的主键一致。
        """
        if article_id not in self.clusters:
            raise ValueError("unknown article_id: %r" % (article_id,))
        want = set(member_ids)
        cl = self.clusters.pop(article_id)
        moving = [m for m in cl["members"] if m["id"] in want]
        remaining = [m for m in cl["members"] if m["id"] not in want]
        if not moving:
            self.clusters[article_id] = cl
            raise ValueError("no members matched: %r" % (member_ids,))

        def build(members, event):
            keys = set().union(*(set(m["keys"]) for m in members))
            canonical = self._canonical(keys)
            return {
                "article_id": _article_id(canonical),
                "canonical_key": canonical,
                "aliases": keys,
                "members": members,
                "history": list(cl["history"]) + [event],
            }

        new_cl = build(moving, {"event": "split_from", "from": article_id,
                                "members": [m["id"] for m in moving],
                                "reason": reason})
        old_cl = build(remaining, {"event": "split_out",
                                   "to": new_cl["article_id"],
                                   "members": [m["id"] for m in moving],
                                   "reason": reason}) if remaining else None
        for c in (new_cl, old_cl):
            if c is not None and c["article_id"] in self.clusters:
                self.clusters[article_id] = cl  # 回滚，保持原状
                raise ValueError("split collides with existing cluster: %r"
                                 % (c["article_id"],))
        if old_cl is not None:
            self._register(old_cl)
        self._register(new_cl)
        for m in moving:
            for n in remaining:
                if m["id"] and n["id"]:
                    self.severed.add(frozenset((m["id"], n["id"])))
        keep = {new_cl["article_id"]}
        if old_cl is not None:
            keep.add(old_cl["article_id"])
        return {
            "split_from": article_id,
            "new_article_id": new_cl["article_id"],
            "remaining_article_id": old_cl["article_id"] if old_cl else None,
            "supersedes": [article_id] if article_id not in keep else [],
        }

    def assignment(self):
        """最终归属：成员记录 id -> article_id（与到达顺序无关）。"""
        return {m["id"]: cl["article_id"]
                for cl in self.clusters.values() for m in cl["members"]}

    def rebuild(self, records):
        """从带 identity 注解的记录（如 store 全量读取）重建归并状态。

        存储注解中的 provenance/aliases/history 足以恢复簇并继续拆分，
        证明错误归并保留了拆回所需信息。
        """
        for rec in records:
            art = rec.get("article_id")
            idn = rec.get("identity") or {}
            if not art or not idn:
                continue
            cl = self.clusters.setdefault(art, {
                "article_id": art, "canonical_key": "",
                "aliases": set(), "members": [], "history": []})
            for k in rec.get("aliases") or []:
                cl["aliases"].add(k)
                self.key_to_article[k] = art
            have = {m.get("id") for m in cl["members"]}
            for m in idn.get("provenance") or []:
                if m.get("id") not in have:
                    cl["members"].append(dict(m))
                    have.add(m.get("id"))
            for h in idn.get("history") or []:
                if h not in cl["history"]:
                    cl["history"].append(h)
            if cl["members"]:
                cl["aliases"] |= set().union(*(set(m["keys"]) for m in cl["members"]))
                for k in cl["aliases"]:
                    self.key_to_article[k] = art
                cl["canonical_key"] = self._canonical(cl["aliases"])
        return self


_RESOLVER = IdentityResolver()


def process(record):
    """流水线阶段：归并文章身份并注解记录（模块级归并器）。"""
    return _RESOLVER.resolve(record)


def reset():
    """重置模块级归并器（测试/重放用）。"""
    global _RESOLVER
    _RESOLVER = IdentityResolver()
    return _RESOLVER
