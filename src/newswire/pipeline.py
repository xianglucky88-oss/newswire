"""薄编排：loader → normalizer→rss_pull→page_fill→identity→publish_time→dedup → keyer → store。

不含业务判断：阶段顺序与时间解释契约见各业务模块 docstring；
阶段若提供 merge(old, new)，落库键冲突时由 store 调用（重复抓取合并）。
"""
import importlib
from . import store as _store
from . import loader as _loader
from .keyer import key_of
from .models import Record

CHAIN_NAMES = ['normalizer', 'rss_pull', 'page_fill', 'identity', 'publish_time', 'dedup']


def run(con=None):
    own = con is None
    con = con or _store.connect()
    try:
        chain, merges = [], []
        for n in CHAIN_NAMES:
            mod = importlib.import_module("." + n, __package__)
            chain.append(mod.process)
            if hasattr(mod, "merge"):
                merges.append(mod.merge)

        def merge_all(old, new):
            for m in merges:
                old = m(old, new)
            return old

        fed = 0
        for raw in _loader.load():
            rec = raw
            for fn in chain:
                rec = fn(rec)
            _store.upsert(con, Record(key=key_of(rec), data=rec),
                          merge=merge_all if merges else None)
            fed += 1
        _store.commit(con)
        return {"fed": fed, "rows": _store.count(con)}
    finally:
        if own:
            con.close()
