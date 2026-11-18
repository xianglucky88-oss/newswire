# newswire — RSS 为线索、页面抓取为补全，构建无重复文章库并还原发布时间。

运行栈：Python 3 + 标准库 `sqlite3`（抓取层可选 `httpx`，缺失时自动回退 `urllib`）
+ `pytest`（离线，无需联网；无 pytest 时可用标准库 runner 回退）

## 运行方式
读 `fixtures/input.json` → `loader` → `normalizer` → 其余处理阶段 → `keyer` → 幂等落 `sqlite`。
```text
make run     # 运行离线流程，输出摄取数量和落库行数
make test    # 运行项目测试，包括流水线、幂等性与 page_fill 离线序列验证
             # （检测到 pytest 用 pytest，否则回退 python3 tests/run_tests.py）
```

## 数据流与模块边界
每个业务阶段保持单一职责；`pipeline.py` 只负责按顺序调用阶段，不在编排层加入业务判断。
业务模块：`page_fill`, `retract`, `publish_time`, `identity`, `rss_pull`, `dedup`。
通用模块：`loader / normalizer / keyer / store / pipeline / cli`。

流水线顺序：`normalizer → rss_pull → page_fill → identity → publish_time → dedup`。

## page_fill：RSS 线索 → 正文页面补全
RSS 条目只有摘要，正文可能在 canonical URL、重定向后的分页页面或附件里。
`page_fill.process(record, *, con=None, fetcher=None, max_pages=10, max_attempts=2)`
把一条线索补全成正文，并把证据链持久化到 SQLite 表 `page_fill_state`
（`store.connect()` 的同一库，按 `keyer.key_of` 的文章身份主键 upsert）。

- **选页**：自走重定向链（不依赖客户端自动跳转，逐跳留证，含环检测）；
  空壳页按 `<link rel="canonical">`/`og:url` 跟随选页（带环保护）；
  按 `rel=next`/“下一页”锚点遍历分页正文并拼接；按后缀与 `rel=attachment`
  收集附件。同页多个正文候选取最长者，全部候选（容器、长度、sha256、
  取舍理由）留证，冲突可回查。
- **完整性与失败状态**：`fill_status ∈ complete | truncated | shell | error`。
  反爬挑战页（403/验证文案/近空页面）判 `shell`；截断标记（阅读全文、
  read-more、省略号）或分页上限判 `truncated`；网络/5xx/429 按 `max_attempts`
  重试后判 `error`。**摘要永远不会被当成正文**，未补全成功时 `body=None`。
- **幂等与接续**：同线索已 `complete` 直接回灌状态、零请求；失败重跑只做
  一次探测，页面签名（状态码 + ETag/Last-Modified + 内容 sha256）未变则
  不追加重复尝试；页面更新或线索变更才追加新尝试，历史尝试全部保留。
  `process_batch` 每条独立提交，半批中断后重启可接续。
- **身份与旧格式**：文章身份沿用上游 `key_of` 契约（`id` 不变）；旧线索格式
  （顶层 `link/url` + `description/excerpt`、`lead/rss/feed` 信封）经同一入口
  规整，证据中以 `legacy_format` 标记。
- **证据链**：输出字段 `body / body_pages / canonical_url / attachments /
  fill_status / fill_reason / fill_evidence`；`fill_evidence` 含线索指纹、
  重定向链、每页候选与截断/空壳判定、历次尝试，支持多候选冲突与补全历史回查。
- **抓取器可注入**：默认 `HttpFetcher`（httpx 优先、urllib 回退，均不自动
  跟随重定向）；测试与离线联调传入带 `get(url) -> RawResponse` 的脚本化
  fetcher 即可，无需联网。

## 持久化与测试
数据通过项目内 SQLite 存储，使用稳定键执行幂等写入；page_fill 状态写入同库
`page_fill_state` 表。测试夹具位于 `tests/`，全部离线回放，运行方式以
`Makefile` 为准。
