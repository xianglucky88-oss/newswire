"""单一职责：从本地 fixtures 读取原始记录（离线摄取入口，不联网）。

``load(path)`` 可显式指定批次文件；默认读取 ``config.FIXTURES``。
读取结果总是“记录列表”，不在这里做任何业务判断。
"""
import json
from pathlib import Path

from .config import FIXTURES


def load(path=None):
    target = Path(path) if path is not None else FIXTURES
    with open(target, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        # 兼容 ``{"records": [...]}`` 形式的批次文件。
        data = data.get("records", [])
    return list(data)
