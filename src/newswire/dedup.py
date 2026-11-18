"""单一职责：去重 —— 同一 article_key 的多次观测合并为一份。

process 本身不丢记录（幂等 upsert 由 keyer+store 完成）；真正的重复消解
在 merge(old, new)：时间解释委托 publish_time.merge_records，
保证重复抓取、迟到补全、崩溃重跑后日期稳定且每次修订来源可查。
"""
from . import publish_time


def process(record):
    return dict(record)


def merge(old, new):
    return publish_time.merge_records(old, new)
