"""薄编排：loader → normalizer → rss_pull → identity → publish_time → retract
→ dedup → keyer → store。不含业务判断。

编排层只负责：

* 按 ``CHAIN`` 顺序调用阶段；
* 为 ``dedup`` 提供运行期上下文（``ctx``），让它在同一批次内复用索引，
  并在每次运行开始时从 store 载入已有文章——这是重入与旧批次回放能收敛
  到同一结果的关键；
* 把 ``dedup`` 返回的文章文档交给 ``keyer`` 派生稳定键；
* 通过 ``store`` 以“一篇文章一个事务”的方式幂等落库。
"""
from __future__ import annotations

import importlib
from typing import Any, Callable, Iterable

from . import loader as _loader
from . import store as _store
from .keyer import key_of
from .models import Record

#: 阶段顺序即数据流顺序。``retract`` 必须在 ``dedup`` 之前，因为 dedup
#: 需要撤稿证据来决定“合并到旧文”还是“新建文章”。
CHAIN_NAMES = ["normalizer", "rss_pull", "identity", "publish_time", "retract", "dedup"]

#: 接受 ``ctx`` 关键字参数的阶段。其余阶段保持 ``process(record)`` 的
#: 单职责签名，便于独立测试。
_CONTEXT_AWARE = {"dedup"}


def _load_chain() -> list[tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]]:
    chain = []
    for name in CHAIN_NAMES:
        module = importlib.import_module("." + name, __package__)
        chain.append((name, module.process))
    return chain


def _call_stage(
    name: str, fn: Callable[..., dict[str, Any]], rec: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    if name in _CONTEXT_AWARE:
        return fn(rec, ctx)
    return fn(rec)


def run(
    con=None,
    *,
    records: Iterable[dict[str, Any]] | None = None,
    source: Any = None,
    reset: bool = False,
):
    """跑一遍流水线。

    ``records`` 可注入替代 loader 的输入（测试/回放用）。``source`` 可传
    批次文件路径或原始列表，等价于 ``records``。``con`` 可传入现有连接
    以叠加到已有库；默认按 ``config.DB_PATH`` 新建连接。``reset=True``
    会清空本库全部表，用于从零重建同一批次的结果。
    """
    if records is None:
        records = source
    if records is None:
        records = _loader.load()
    elif isinstance(records, (str, bytes)) or hasattr(records, "__fspath__"):
        records = _loader.load(records)
    own = con is None
    con = con or _store.connect()
    try:
        if reset:
            _store.reset(con)
        chain = _load_chain()
        ctx: dict[str, Any] = {"con": con}
        fed = 0
        for raw in records:
            rec: dict[str, Any] = raw
            for name, fn in chain:
                rec = _call_stage(name, fn, rec, ctx)
            _store.upsert(con, Record(key=key_of(rec), data=rec))
            fed += 1
        _store.commit(con)
        return {
            "fed": fed,
            "rows": _store.count(con),
            "versions": _store.version_count(con),
            "events": _store.event_count(con),
        }
    finally:
        if own:
            con.close()
