# Agentic RAG 流程审计更新

日期：2026-05-13

范围：基于 `AGENTS.md`、`.agent/skills/agentic_debug_patterns/SKILL.md`、既有 trace 目录 `/Users/wuchengke/Desktop/agentic_e2e_traces/20260513_183340`，以及额外运行的典型用例结果。

额外 trace 输出目录：

- `/Users/wuchengke/Desktop/agentic_e2e_traces/20260513_190749_extra`
- `/Users/wuchengke/Desktop/agentic_e2e_traces/20260513_210646_extra_batch`

## 结论摘要

这套 agentic RAG 的总体方向是成立的：外层 workflow 可以把复杂问题拆成多个 retrieve/synthesize step 并并发执行；内层 retrieval agent 能基于 KG 选文档、树导航、发现补充路径，并把 connected image/table 内嵌回证据树。尤其是全局图片/图表类问题，已有路径可以通过 `connect_to` 找回资源所属文本 section。

但从 harness 工程师和真实用户视角看，目前仍有几个核心逻辑风险：

1. 图表资源的“证据渲染归属”和“返回引用归属”不一致。渲染树里通常能用 `connect_to` 找到底层 owner section，但 `referenced_chunks`/citation 仍可能显示物理路径 `Root`，这会直接破坏用户理解图表出处。
2. discovery merge 过于积极。即使 BFS 已经 `STOP`，后置 discovery 仍会把深层或邻近年份路径并入证据，导致 outline 类问题和窄 section 问题出现噪声。
3. 预算和状态分类混淆。无证据问题会被包装成 `budget_stop`，掩盖真实原因；同时 bootstrap/revision 小预算耗尽时，整体 wallet 仍可能很充足，用户看到的失败原因不准确。
4. 多文件/多 step 并发在外层有效，但单 step 内的多 doc 导航仍偏串行；更重要的是 `discovery_auto` 会把弱相关文档强行并入，容易在跨年份、跨主题问题上污染预算和证据。
5. 回答 JSON 解析不够稳健。`attempt_answer` 返回含换行的 JSON-like 文本时会解析失败，单步用户可能看到 JSON wrapper。
6. trace DB schema 与 ORM 不一致，导致 agentic trace 入库失败，削弱 harness 可观测性。

## 本次补跑用例

### T1_Outline_Extra

Query：

> 民生证券这份利率专题研报的整体结构是什么？包含哪些主要章节？

结果：

- Router：`workflow_single_step`
- LLM calls：4
- refs：13
- elapsed：约 13s
- action：`kg_document_select -> navigate -> discovery_select -> attempt_answer`

观察：

- `navigate` 在 root 层正确选择 `STOP`，这对“整体结构/主要章节”类问题是合理的。
- 但后续 `discovery_select` 又选入了深层路径，如 `2 阶段性调整.../2.1.1 基本面企稳` 和 `5、2024：“资产荒”的极致演绎`。
- 最终 evidence 约 7395 chars，answer 只有约 149 chars，说明证据明显过量。
- `referenced_chunks` 里部分 image/table 的 section 显示为 `Root`，但 evidence tree 实际把它们挂在更具体 leaf section 下。

判断：

这是 discovery merge 策略的问题。对 root outline 查询，BFS 已经完成任务后，不应默认再并入深层 discovery 结果。否则用户问“目录结构”，结果引用中会混入某些深层图表，影响可信度。

### T2_Deep_Section_Extra

Query：

> 2016年债市走牛的几个阶段中，机构行为是如何推动行情演绎的？有哪些相关图表说明？

结果：

- Router：`workflow_single_step`
- LLM calls：4
- refs：51
- elapsed：约 29s
- evidence：约 22960 chars
- wallet context：`TIGHT`

观察：

- `navigate` 选择了 `NAVIGATE`，并带 `FIND_IMAGES`、`FIND_TABLES`，方向正确。
- 但 root scope 的 asset tool 拉入了过多全局资源；同时 discovery 又选中父级 `1、2016：机构行为助推行情演绎`，导致 hydration 范围扩大。
- evidence 里实际有图2、图3、图4、图5等图题和图片描述，但模型回答中仍说“未提供图表具体标题/编号”。
- refs 达到 51，包含不少 2018、2019、2023、2024 等非目标年份资源。
- 2016 相关图片在引用元数据中仍有 `section=Root` 的情况，虽然它们通过 `connect_to` 在 evidence 中被放回了具体 section。

判断：

这是窄 section + 图表问题的典型失败形态：导航方向正确，但工具作用域过宽、discovery 过宽、证据渲染噪声大，导致模型虽然拿到了图表，却没有稳定提取图题和归属。

### T3_Compare_Extra

Query：

> 对比2024年和2025年AI安全市场规模，并结合证据给出变化原因。

结果：

- Router：`workflow_decomposed`
- LLM calls：27
- refs：1
- elapsed：约 38s
- plan：s1 查 2024 市场规模，s2 查 2025 市场规模，s3 查变化原因，s4 synthesize

观察：

- 外层 workflow 确认可以并发执行多个 retrieve step，三个 retrieve step 的 KG select 和 navigate 调用是交错发生的。
- 三个 retrieve step 最终都进入 revision，然后以 `budget_stop` 结束。
- 总体 wallet 仍有大量剩余，但 bootstrap/revision 局部预算先被耗尽，最终对用户呈现为“预算停止”。
- 实际语义更接近：KB 中缺少可支撑 2024/2025 AI 安全市场规模对比的证据。
- `discovery_auto` 因年份词匹配，把债券研报等弱相关文档带入候选，造成预算消耗和路径污染。

判断：

这是预算状态和无证据状态混淆。对用户来说，“知识库没有足够证据”和“预算不够”是两类完全不同的反馈；当前状态分类会误导用户，也会误导 harness 判断。

## 核心问题清单

### 1. 图表资源归属在 citation 层丢失

涉及核心设计：

- 每个独立图表/图片/表格都应通过 `connect_to` 找到底层 section 归属。
- 物理资源 chunk 的 `path` 可能是 `images/...` 或 `tables/...`，甚至 DB section 可能挂在 `Root`。
- 逻辑归属应以 text chunk 的 `metadata.connect_to[].target` 为准，`target` 指向 image/table chunk_id。

当前表现：

- evidence tree 渲染阶段多数情况下能用 owner path 把资源挂回 leaf section。
- 但最终 `referenced_chunks`/citation 仍可能使用资源 chunk 自身的 `section_path`，因此显示 `Root`。

影响：

- 用户看到图表出处为 `Root`，无法判断它属于哪个章节。
- 对图表比较、章节归因、报告复核非常不友好。
- 这和“每个独立图表都有 `connect_to` 找到一个底层 section 归属”的设计要求冲突。

建议：

- citation/ref 组装时优先使用 `owner_section_path`，只有不存在时才回退到物理 `section_path`。
- 返回结构中建议同时保留：
  - `owner_section_path`：逻辑归属，用于用户展示和排序。
  - `physical_section_path`：数据库/资源物理挂载位置，用于调试。
  - `connect_to_source_chunk_id`：是哪一个 text chunk 证明了该资源归属。
- 对 image/table 引用增加断言：若存在 `connect_to` owner，则展示 section 不应为 `Root`。

### 2. discovery merge 对 STOP 和 outline 查询缺少门控

当前表现：

- T1 root outline 查询已经由 `navigate` 正确 `STOP`。
- 后续 `discovery_select` 仍并入深层路径和资源。

影响：

- 简单结构问题证据膨胀。
- 引用混入深层内容，用户会怀疑答案是不是依据了错误章节。
- 预算被无谓消耗。

建议：

- 对 outline/structure/catalogue 类意图设置 discovery gate：
  - 若 root STOP 且问题不要求“细节/图表/数据”，跳过 discovery hydration。
  - 或只允许 discovery 返回 top-level structural sections，不 hydrate leaf content/assets。
- `discovery_select` 的 prompt 应明确区分：
  - structure query：只补结构遗漏。
  - evidence query：可补 leaf 内容。
  - asset query：可补 image/table。

### 3. 图表工具作用域过宽

当前表现：

- T2 中 `NAVIGATE + FIND_IMAGES/FIND_TABLES` 方向正确，但 root 或父级 scope asset extraction 拉入大量非目标年份图表。
- 后续 trimming 虽然会删一部分，但已经消耗 context 和模型注意力。

影响：

- 窄问题变成大范围 evidence dump。
- 模型可能拿到正确图题却没有稳定使用，反而回答“没有具体标题”。
- refs 过多，前端引用列表不可读。

建议：

- 当 action 为 `NAVIGATE` 且有 selected leaf paths 时，asset tools 默认只对 selected paths 或其 owner-linked assets 生效。
- 只有 action 为 root `STOP` 且 query 明确要求“列出全部图表/图片/表格”时，才允许文档级全量 asset pull。
- 对 `FIND_IMAGES/FIND_TABLES` 的输出增加 owner filter：资源必须能通过 `connect_to` 归属到当前 selected subtree。

### 4. 预算分配与状态管理需要区分技术预算和语义失败

当前表现：

- T3 三个 retrieve step 最终都是 `budget_stop`。
- 但总 wallet 明显还有剩余，真正失败原因是没有足够证据。
- bootstrap/revision 局部预算耗尽被升级成 step 级 budget stop。

影响：

- 用户会以为“系统钱/上下文不够”，而不是“知识库无证据”。
- harness 也难以判断是预算策略问题、检索召回问题还是 KB 数据缺失。

建议：

- step status 拆分：
  - `not_found_no_evidence`
  - `not_found_low_confidence`
  - `budget_exhausted_bootstrap`
  - `budget_exhausted_context`
  - `budget_exhausted_total`
- synthesize 时保留每个 retrieve step 的 semantic reason，不要只看 stop_reason 字符串。
- revision loop 中，如果第一轮和第二轮文档选择高度重复且 verdict 是“KB 缺证据”，应提前停止，避免继续烧 bootstrap。
- `BudgetWallet` 的 reclaimed budget 如果暂不重分配，snapshot 文案应避免暗示这些预算已重新可用。

### 5. Planner 缺少 KB inventory，导致 plan reasoning 误报

当前表现：

- T4 中 planner reasoning 出现 “knowledge base is empty”。
- 实际 trace 中 KB 并不为空。

判断：

`QueryPlanner.plan()` 支持 `kb_total_docs/kb_total_chunks` 参数，但 workflow 调用路径没有传入真实 inventory，默认值为 0。

影响：

- plan reasoning 不可信。
- 对调试和用户解释都很危险。

建议：

- `_load_or_plan()` 前读取当前 namespace 的 KB inventory，并传给 planner。
- workflow plan cache key 应包含 KB version 或文档集合 fingerprint，否则 KB 更新后可能复用旧 plan。

### 6. `attempt_answer` JSON 解析不稳健

当前表现：

- T4 中 `attempt_answer` 返回 JSON-like 内容，但因 raw newline 或不合规转义导致 parse 失败。
- parse 失败后逻辑把原始字符串当作 DONE answer。

影响：

- 单步用户可能看到 `{"status":"DONE","answer":...}` wrapper。
- synth step 可能能“洗掉”问题，但 single-step 场景会暴露。

建议：

- 增加 tolerant JSON repair，只修复回答字段中的裸换行/控制字符。
- 如果解析失败且文本明显以 JSON object 开头，不应直接 `DONE raw`，而应降级重试或抽取 `answer` 字段。

### 7. trace DB schema 与 ORM 不一致

当前表现：

- `retrieval_runs.parent_run_id/workflow_step_id/workflow_plan` 在 ORM 中存在。
- alembic migration 中未创建这些列。
- trace create_run 报 `UndefinedColumnError`。

影响：

- DB trace 不可用。
- harness 只能依赖 Markdown trace，无法做结构化聚合和回归分析。

建议：

- 补 migration。
- 增加一个轻量 schema contract test，覆盖 `RetrievalTraceRecorder.create_run()`。

### 8. 多文件并发导航的现状

已确认：

- 外层 workflow retrieve steps 使用 topological batch 并发执行。
- T3 中多个 retrieve step 的 KG select/navigate 调用交错，说明并发有效。

风险：

- 单个 retrieve step 内 selected docs 仍偏串行。
- `discovery_auto` 追加的弱相关文档没有足够 domain guard，T3 因年份匹配引入了债券研报。

建议：

- 对 `discovery_auto` 文档追加设置最低 domain relevance：
  - 文档 title/summary/keywords 至少命中主题实体。
  - 或要求 bottom chunk 与 query 的非时间词、非通用词有足够 overlap。
- 单 step 多 doc 可考虑并发，但要先修好 doc relevance guard，否则并发只会更快地放大噪声。

## 遗留与冗余代码观察

### Legacy retrieval 路径仍和 agentic 路径混杂

`run_retrieval_query()` 中同时存在 agentic workflow 和 legacy 3-channel RRF 排序/graph routing。若 agentic 已是主路径，建议把 legacy 路径隔离为明确 fallback，避免后续改动时误改两套逻辑。

### 旧 graph/discovery helper 有疑似未使用分支

`agentic/orchestrator.py` 附近存在 `_grep_discover_document_ids`、`_expand_by_edges` 等老式发现逻辑痕迹。若主流程已经切到 bottom discovery + KG select，应确认这些 helper 是否仍被调用；未调用则标记删除或迁移到测试辅助。

### path dedup 当前依赖“一叶一文本 chunk”隐含前提

当前 `_hydrate_paths_to_rows` 用 path-level `seen_paths` 是安全的，因为解析模型近似保持“一 leaf section 一个 text chunk”。但如果未来 parser 把一个 leaf section 拆成多个 text chunks，path-level dedup 会丢内容。

建议：

- 在注释和测试中写明该前提。
- 或把 dedup key 改成 `(document_id, section_path, chunk_id)`，再在 render 层控制同 section 合并。

## 建议优先级

P0：

1. 修复 image/table citation 归属：优先展示 `connect_to` owner section，不再把有 owner 的图表显示成 `Root`。
2. 修复 trace DB migration，恢复 harness 结构化观测。
3. 修复 `attempt_answer` JSON parse fallback，避免把 wrapper 暴露给用户。

P1：

1. 对 root STOP/outline query 增加 discovery gate。
2. 收紧 asset tool 作用域：`NAVIGATE + selected paths` 时只找 selected subtree 的 connected assets。
3. 拆分 `budget_stop` 与 `not_found` 状态，synthesize 阶段保留真实失败原因。

P2：

1. 给 planner 传真实 KB inventory，并把 KB fingerprint 纳入 plan cache key。
2. 给 `discovery_auto` 增加 domain relevance guard。
3. 清理 legacy helper 和未使用 discovery/graph 分支。
4. 为 path dedup 增加未来多 chunk leaf 的保护测试。

## 建议回归用例

1. Outline STOP 不应 hydrate 深层 leaf：
   - Query：`民生证券这份利率专题研报的整体结构是什么？包含哪些主要章节？`
   - 断言：refs 中不应出现大量 image/table；深层 section 不应被 discovery 自动并入。

2. 2016 section 图表归属：
   - Query：`2016年债市走牛的几个阶段中，机构行为是如何推动行情演绎的？有哪些相关图表说明？`
   - 断言：所有相关 image/table citation 的展示 section 应为 2016 底层 section，而不是 `Root`。

3. 全量图表查询：
   - Query：`列出AI安全大模型报告中所有的图表和图片，并简要描述每张图的内容。`
   - 断言：允许 root/global asset pull，但每个独立图表仍应有 owner section；确实无底层 owner 的封面/前言图要显式标记为 document-level。

4. KB 无证据查询：
   - Query：`对比2024年和2025年AI安全市场规模，并结合证据给出变化原因。`
   - 断言：返回状态应是 no evidence / insufficient evidence，而不是 generic `budget_stop`。

5. Planner inventory：
   - 构造非空 KB。
   - 断言 planner reasoning 不得出现 “knowledge base is empty”。

## 代码落点索引

- Workflow orchestration：`packages/shared-python/shared/services/retrieval/workflow/orchestrator.py`
- Planner：`packages/shared-python/shared/services/retrieval/workflow/planner.py`
- Workflow budget wallet：`packages/shared-python/shared/services/retrieval/workflow/wallet.py`
- Inner agent orchestrator：`packages/shared-python/shared/services/retrieval/agentic/orchestrator.py`
- Inner agent tools：`packages/shared-python/shared/services/retrieval/agentic/tools.py`
- Answer policy / JSON parse：`packages/shared-python/shared/services/retrieval/agentic/policy.py`
- Navigation tree render：`packages/shared-python/shared/services/retrieval/agent_navigate.py`
- Retrieval entry / hydration：`packages/shared-python/shared/services/retrieval/app_service.py`
- Retrieval trace：`packages/shared-python/shared/services/retrieval/agentic/trace.py`
- ORM retrieval tables：`packages/shared-python/shared/models/database/document.py`
- Migration：`apps/api/alembic/versions/e5f6a7b8c9d0_add_agentic_retrieval_tables.py`
- Debug harness：`apps/worker/debug_agentic_e2e.py`

