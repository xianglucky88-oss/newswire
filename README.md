# newswire — RSS 为线索、页面抓取为补全，构建无重复文章库并还原发布时间。

运行栈：Python 3.14 + 标准库 `sqlite3` + `httpx` + `pytest`（离线，无需联网）

## 运行方式
读 `fixtures/input.json` → `loader` → `normalizer` → 其余处理阶段 → `keyer` → 幂等落 `sqlite`。
```text
make run     # 运行离线流程，输出摄取数量和落库行数
make test    # 运行项目测试，包括流水线和幂等性检查
```

## 数据流与模块边界
每个业务阶段保持单一职责；`pipeline.py` 只负责按顺序调用阶段，不在编排层加入业务判断。
业务模块：`page_fill`, `retract`, `publish_time`, `identity`, `rss_pull`, `dedup`。
通用模块：`loader / normalizer / keyer / store / pipeline / cli`。

## 模块说明
业务模块按独立阶段组织，通用模块负责加载、规范化、主键、存储和编排。
流水线按配置顺序调用处理阶段，输入输出均遵循项目定义的记录结构。

## 身份归并（identity）
`identity` 先判定记录指向哪个文章对象，再把文章身份（`article_id`）与别名
（`aliases`）随记录传给 `publish_time` / `dedup` / `store`；修订事件判定仍在下游。
- 强键命中即归并：规范化 URL（去跟踪参数、去 fragment、查询参数排序）、
  `canonical_url`、重定向 `final_url`、`guid`、显式关联
  （`same_as` / `translation_of` / `reposted_from`）。
- 弱信号累计：共享附件 0.6、标题规范化一致 0.5、标题相似(≥0.8) 0.4、同源 0.1；
  合计 ≥ 1.0 才自动归并 —— 单一弱信号（哪怕标题全同）不足以归并；
  0.5–1.0 的候选记入 `identity.rejected`，保守分离的边界可审计。
- 归并全程留证（`identity.evidence` / `provenance` / `history`）；错误归并可用
  `IdentityResolver.split()` 拆回，双方保留历史来源，拆分对不再自动归并。

## 持久化与测试
数据通过项目内 SQLite 存储，使用稳定键执行幂等写入；测试夹具位于项目测试目录，运行方式以 `Makefile` 为准。
