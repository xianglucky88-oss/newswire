"""离线 CLI。

用法::

    python -m newswire.cli                # 跑默认 fixtures 批次
    python -m newswire.cli --fixture FILE # 跑指定批次文件
    python -m newswire.cli --db FILE      # 指定 sqlite 数据库
    python -m newswire.cli --reset        # 先清空数据库再跑（从零重建）

输出摄取值、文章行数、版本数与事件数，便于人工核对重入是否收敛。
"""
import argparse
import json
import sys

from . import pipeline, store
from .config import DB_PATH


def main(argv=None):
    parser = argparse.ArgumentParser(prog="newswire", description="offline newswire pipeline")
    parser.add_argument("--fixture", help="批次 JSON 文件（默认 fixtures/input.json）")
    parser.add_argument("--db", default=str(DB_PATH), help="sqlite 数据库路径")
    parser.add_argument("--reset", action="store_true", help="先清空数据库再摄取")
    args = parser.parse_args(argv)

    con = store.connect(args.db)
    try:
        result = pipeline.run(con, records=pipeline._loader.load(args.fixture), reset=args.reset)
    finally:
        con.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
