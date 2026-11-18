# newswire — RSS 为线索、页面抓取为补全，构建无重复文章库并还原发布时间。

运行栈：Python 3.14 + 标准库 `sqlite3` + `httpx` + `pytest`（离线，无需联网）

## 运行方式
读 `fixtures/input.json` → `loader` → `normalizer` → 其余处理阶段 → `keyer` → 幂等落 `sqlite`。
```text
make run     # 运行离线批次，输出摄取数、文章行数、版本数与事件数
make test    # 运行项目测试，包括流水线、幂等性和重复/修订识别
make demo    # 跑 fixtures/scenario.json：转载/改版/撤稿重发/新文全场景
```
CLI 支持指定批次与数据库，便于重入与回放检查：
```text
PYTHONPATH=src python3 -m newswire.cli --fixture fixtures/scenario.json --db /tmp/a.db --reset
PYTHONPATH=src python3 -m newswire.cli --fixture fixtures/scenario.json --db /tmp/a.db
```

## 数据流与模块边界
每个业务阶段保持单一职责；`pipeline.py` 只负责按顺序调用阶段，不在编排层加入业务判断。
业务模块：`page_fill`, `retract`, `publish_time`, `identity`, `rss_pull`, `dedup`。
通用模块：`loader / normalizer / keyer / store / pipeline / cli`。

## 重复与修订识别
`identity` 先算出身份证据：规范 URL、标题/正文摘要、来源（含联合发布来源）与身份别名。
`publish_time` 把“发布时间”和“更新时间”分开，给出发布区间与抓取时间。
`retract` 识别撤稿/更正声明及其目标。`dedup` 只消费这些证据，输出文章文档：
版本链（不可变内容版本）、来源集合、关系事件（转载/改版/撤稿/重发/观察）与状态。

判定顺序：撤稿目标优先 → 规范 URL 相同 → 内容摘要相同 → 内容近似 → 新文。
`keyer` 由文章文档派生稳定键，`store` 在一篇文章一个事务内幂等写入
`records`（文章）、`aliases`（身份别名）、`versions`（版本链）、`events`（关系证据）。

### 取舍规则：内容近似但发布时间不同
- 内容**完全一致**（摘要相同）：与任一已有版本的发布时间相距在窗口内
  （默认 ±24h，`MAX_WINDOW_HOURS=72`）→ 同一篇，记为**转载**；全部超出窗口
  → 视作**真正的新文**，但仍留下指向旧文的 `republication` 证据。
- 内容**近似但不完全一致**：只在窗口内合并（记为**改版**）；窗口外一律新文，
  避免把同主题的系列报道吸成一篇文章。
- 任一方缺少可靠的媒体发布时间（例如只有抓取时间 `ts`）时，不做“超出窗口
  即新文”的判断：宁可合并为转载，也不因抓取时间差异凭空新增文章。
- 缺少正文的副本（例如 RSS 摘要）不会追加版本，也不会仅凭相同标题被当作新文。
- 撤稿声明指向的旧文一定合并，即使发布时间相差很远；“撤稿后重发”使用与旧文
  相同的内容时也不会新增重复版本。

### 时间与重入
- 仅修改 `updated`（媒体更新时间）不会新增文章、版本或可见关系：只补一条
  `observed` 观察证据。
- 重入或旧批次回放从 store 重建索引；文章行数与版本数收敛。回放早于撤稿的
  记录不会把已撤稿文章复活（记为 `stale_observation`）。
- 部分写入在记录边界中断：已提交记录保持完整，重跑剩余记录后与一次性全量
  运行得到相同的文章、版本、状态与关系证据（额外的 `observed` 仅表示重跑过）。

## 持久化与测试
数据通过项目内 SQLite 存储，使用稳定键执行幂等写入；测试夹具位于项目测试目录，运行方式以 `Makefile` 为准。
