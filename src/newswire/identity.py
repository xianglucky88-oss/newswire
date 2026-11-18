"""单一职责：文章身份 —— 派生与时间无关的稳定主键 article_key。

身份只依赖 canonical_url（或 src+guid），不依赖任何时间字段：
同一篇文章被重复抓取、迟到补全或修订后，主键保持不变，
publish_time 的合并与去重才能挂在同一身份上。
"""
import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAMS = {"fbclid", "gclid"}


def canonical_url(url: str) -> str:
    u = urlsplit(str(url).strip())
    scheme = (u.scheme or "https").lower()
    host = (u.hostname or "").lower()
    port = u.port
    netloc = host + (f":{port}" if port and port not in (80, 443) else "")
    path = u.path.rstrip("/") or "/"
    query = urlencode(sorted(
        (k, v) for k, v in parse_qsl(u.query)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ))
    return urlunsplit((scheme, netloc, path, query, ""))


def process(record):
    rec = dict(record)
    if rec.get("url"):
        rec["canonical_url"] = canonical_url(rec["url"])
        rec["article_key"] = "u:" + hashlib.sha256(
            rec["canonical_url"].encode()).hexdigest()[:16]
    elif rec.get("guid"):
        rec["article_key"] = "g:" + hashlib.sha256(
            f"{rec.get('src', '')}|{rec['guid']}".encode()).hexdigest()[:16]
    return rec
