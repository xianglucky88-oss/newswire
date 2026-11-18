# newswire — RSS 为线索、页面抓取为补全，构建无重复文章库并还原发布时间。

运行栈：Python 3.11+ + 标准库 `sqlite3` + `httpx` + `pytest`（离线，无需联网）

## 运行方式
读 `fixtures/input.json` → `loader` → `normalizer` → 其余处理阶段 → `keyer` → 幂等落 `sqlite`。
```text
make run     # 运行离线流程，输出摄取数量和落库行数
make test    # 运行项目测试，包括流水线和幂等性检查
```

## 数据流与模块边界
每个业务阶段保持单一职责；`pipeline.py` 只负责按顺序调用阶段，不在编排层加入业务判断。
业务模块：`rss_pull`, `page_fill`, `publish_time`, `identity`, `dedup`, `retract`。
通用模块：`loader / normalizer / keyer / store / pipeline / cli`。

## 时间解释契约（传递关系）
```
rss_pull   → time_clues.rss        {published, updated, seen_at}   # 只取线索
page_fill  → time_clues.page_meta  {published, modified, tz}       # 迟到补全：本轮 miss 不造时间
           → time_clues.body       {date, relative, tz, ref}
identity   → article_key           # 与时间无关的稳定主键（canonical_url / src+guid）
publish_time → published_at / revised_at / publish_time 块          # 唯一解释入口
dedup      → merge(old, new)       # 委托 publish_time.merge_records，键冲突时由 store 调用
```
- `published_at`：采用的发布时间（UTC ISO8601 或 `None`）。**无法还原时保持 `None`，绝不回填处理当天。**
- `revised_at`：最近修订时间。修订时间永远不作为 `published_at`，也不参与"最新"排序，
  避免旧新闻因被编辑而挤到最新位置。
- `publish_time` 块：`candidates`（每个候选含 source/role/kind/raw/value/confidence/note）、
  采用值、`basis`/`basis_code`（置信依据）、`revisions`（合并导致的采用值变更，来源可查）。

## 置信模型与冲突处理
来源基础置信度：`page_meta 0.95 > rss 0.85 > body 0.70 > legacy 0.60`；
相对时间 ×0.7，裸时间缺时区按 UTC 假设 ×0.9（留 `tz-assumed-utc` note）。
同一瞬时的不同时区表达归一化后不算冲突；绝对时间候选相差 >2h 记为冲突，
采用高置信来源并在 `basis` 中列明双方（`adopted-with-conflict`）。

## 排序规则（`publish_time.sort_feed`，稳定且可解释）
1. 有发布时间：按发布时间倒序（绝对优先于相对，同值按 `article_key` 稳定）；
2. 仅有修订时间：整层排在其后（修订不参与"最新"）；
3. 无法还原：垫底，按主键稳定。

## 稳定性保证
`publish_time.process` 是纯函数：重复抓取、迟到补全、持久化中断恢复后重跑，
`published_at` 与 `revisions` 保持不变；合并时 `time_clues` 按来源取并集，
信息较少的重复抓取不会把已还原的发布时间降级。旧记录的裸字段
（`ts/date/updated/modified/published_at`）按 `legacy` 线索兼容读取。

## 持久化与测试
数据通过项目内 SQLite 存储，使用稳定键执行幂等写入；测试夹具位于项目测试目录，运行方式以 `Makefile` 为准。
