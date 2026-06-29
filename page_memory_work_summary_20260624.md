# Page-Memory 流程改造与测试进度汇总

日期：2026-06-24  
文档：`SJSYJ-SC-2024 企业制度汇编（上册）.pdf`  
当前 debug 目录：`/Users/wuchengke/.knowhere/_debug_parse/SJSYJ-SC-2024 企业制度汇编（上册）.pdf/page_memory`

## 1. 背景问题

这轮工作从“检查解析”对话中的异常开始：

```text
The encrypted content Hand...run. could not be verified.
Reason: Encrypted content could not be decrypted or parsed.
traceid: f63bf5fe74f7bb1859be513dd7e54b22
```

当时 debug 目录里只有：

```text
page_memory_fine_hierarchy.json
```

但没有看到 PAGE-TAG 结果，也无法判断流程到底跑到哪一步。随后确认核心问题不是单个文件缺失，而是 page-memory 的中间产物、trace、scope 组织方式和生产链路不一致，导致 debug 不可控、不透明。

## 2. 已确认的目标流程

我们对齐后的 page-memory 生产/测试流程是：

1. 先做 DOC_PROFILE，抽取文档页特征和 TOC hierarchy。
2. 基于粗 TOC hierarchy 建立 coarse scopes。
3. 对每个 coarse scope 构建 fine hierarchy。
4. 只在 fine hierarchy 覆盖的页面范围内做 PAGE-TAG。
5. 如开启图表资产提取，则同样在 hierarchy 覆盖范围内抽取资产。
6. 最后汇总生成顶层 `hierarchy.json`、`page_tags.json`、`assets.json`，并继续支持后续 `chunks.json`、`doc_nav.json`、`manifest.json` 生成。

测试链路保持生产形状，但可以用 `--fat-only` 只选择最大的粗 scope 进行快速验证。

## 3. 已落地的主要改造

### 3.1 Trace 统一到顶层

已取消 `_doc_agent/trace.json` 的重复落盘，统一写到：

```text
page_memory/trace.json
```

`_doc_agent/` 目前只保留 DOC_PROFILE/TOC 相关原始调试材料：

```text
_doc_agent/anatomy_map.json
_doc_agent/toc_hierarchies.json
```

### 3.2 中间产物精简

当前 stop-at fine 的落盘结构已经收敛为：

```text
page_memory/
  trace.json
  hierarchy.json
  page_tags.json
  _doc_agent/
    anatomy_map.json
    toc_hierarchies.json
  scopes/
    p225-301/
      scope.json
      fine_hierarchy.json
      page_tags.json
```

不再输出大量重复的临时 JSON，例如旧的 `coarse_tag_scope.json`、`page_memory_fine_hierarchy.json`、`page_tags_pre_hierarchy.json` 等。

### 3.3 Scope 目录改为页码范围

scope 目录已从 hash/UUID 风格改为页码范围：

```text
scopes/p225-301/
```

这样 debug 时可以直接看出该 scope 覆盖的页域。

### 3.4 `hierarchy.json` 改为可读树形优先

顶层 `hierarchy.json` 和 scope 内 `fine_hierarchy.json` 都改成：

```json
{
  "HIERARCHY": {},
  "nodes": [],
  "stats": {}
}
```

其中 `HIERARCHY` 是第一字段，形态对齐最终 `manifest.json` 里的 `HIERARCHY` 字段，方便直接肉眼 debug。

scope 内的 `fine_hierarchy.json` 额外包含：

```json
{
  "scope": {}
}
```

机器流程仍可继续使用 `nodes`。

### 3.5 页域字段精简

scope 和 trace 中不再同时记录 `page_ranges` 和完整 `pages` 列表。

当前约定：

```json
{
  "document_page_count": 423,
  "page_count": 77,
  "page_ranges": [[225, 301]]
}
```

保留逐页 `page_index` 的地方仅限实体数据，例如：

```text
page_tags.json
assets.json
```

因为这些文件本身就是逐页/逐资产记录。

## 4. 当前测试进度

本次从原始 PDF 重新开始测试：

```text
/Users/wuchengke/Desktop/temp/test_docs/SJSYJ-SC-2024 企业制度汇编（上册）.pdf
```

执行目标：

```text
从头跑到最大粗 scope 的 fine hierarchy
```

实际命令：

```bash
uv run python apps/worker/scripts/debug_page_memory.py \
  --file '/Users/wuchengke/Desktop/temp/test_docs/SJSYJ-SC-2024 企业制度汇编（上册）.pdf' \
  --fat-only \
  --stop-at fine
```

结果：成功，最终状态为：

```text
stopped_at_fine
```

### 4.1 DOC_PROFILE 结果

```text
page_count: 423
toc_pages: [5, 6, 228]
native TOC TitleNode: 58
native TOC leaf nodes: 44
```

注意：C4 skeleton 阶段日志显示嵌入式目录页 `[228]` 被当前 global TOC 选择逻辑跳过：

```text
embedded_toc_region_outside_front_cluster
```

这属于后续可优化点。

### 4.2 C4 Skeleton 定位

```text
skeleton_count: 25
elapsed: 279.36s
```

残余定位阶段使用了多轮小窗口渲染 + VLM 确认，耗时较长。

### 4.3 最大粗 scope 选择

`--fat-only` 本次选中：

```text
scope_id: p225-301
page_ranges: [[225, 301]]
page_count: 77
coarse skeletons before fine: 1
```

### 4.4 Fine hierarchy 结果

title detection：

```text
77 VLM calls
54 titles found
36 pages with observed_titles
```

fine hierarchy：

```text
1 -> 52 skeletons
elapsed: 110.5s
```

最终 `hierarchy.json`：

```json
{
  "stats": {
    "node_count": 52,
    "page_count": 77,
    "page_ranges": [[225, 301]],
    "max_depth": 6
  }
}
```

顶层 `HIERARCHY` 当前顶级节点：

```text
安全类
```

## 5. 当前已验证的文件

### 5.1 顶层 `hierarchy.json`

路径：

```text
page_memory/hierarchy.json
```

检查结果：

```text
top_keys: HIERARCHY, nodes, stats
node_count: 52
page_count: 77
page_ranges: [[225, 301]]
max_depth: 6
```

### 5.2 Scope `scope.json`

路径：

```text
page_memory/scopes/p225-301/scope.json
```

检查结果：

```json
{
  "scope_id": "p225-301",
  "strategy": "fat_only_coarse_scope:refined",
  "document_page_count": 423,
  "page_count": 77,
  "page_ranges": [[225, 301]],
  "skeleton_count": 52
}
```

确认：没有 `pages` 长列表。

### 5.3 Scope `fine_hierarchy.json`

路径：

```text
page_memory/scopes/p225-301/fine_hierarchy.json
```

检查结果：

```text
top_keys: HIERARCHY, nodes, stats, scope
node_count: 52
page_count: 77
page_ranges: [[225, 301]]
max_depth: 6
```

### 5.4 `trace.json`

路径：

```text
page_memory/trace.json
```

检查结果：

```text
final_status: stopped_at_fine
stage_count: 10
summary.page_count: 423
summary.scope_id: p225-301
```

最后几个 stage 的 page_info 均为 compact range：

```text
C4.coarse_scope          page_count=77 page_ranges=[[225, 301]]
C1.render_pages.coarse   page_count=77 page_ranges=[[225, 301]]
C2.page_plan.coarse      page_count=77 page_ranges=[[225, 301]]
C3b.title_detection      page_count=77 page_ranges=[[225, 301]]
C4b.fine_hierarchy       fat_leaf.page_count=77 fat_leaf.page_ranges=[[225, 301]]
```

## 6. 已跑过的代码检查

最近一次相关检查通过：

```bash
uv run ruff check \
  apps/worker/app/services/page_memory/memory_service.py \
  apps/worker/app/services/page_memory/fine_hierarchy.py \
  apps/worker/scripts/debug_page_memory.py
```

```bash
python -m py_compile \
  apps/worker/app/services/page_memory/memory_service.py \
  apps/worker/app/services/page_memory/fine_hierarchy.py \
  apps/worker/scripts/debug_page_memory.py
```

```bash
uv run pytest \
  apps/worker/tests/contract/test_page_memory_fine_hierarchy_contract.py \
  apps/worker/tests/contract/test_page_memory_node_assembler_contract.py \
  apps/worker/tests/contract/test_document_agent_budget_contract.py \
  -q
```

结果：

```text
17 passed
```

## 7. 当前待讨论/后续优化点

### 7.1 `parent_paths` 仍偏长

`scope.json` 里目前仍保留 `parent_paths`，虽然不是 page 长列表，但对 debug 阅读来说有些臃肿。

可选优化：

```json
{
  "root_path": "...",
  "parent_path_count": 14
}
```

完整 `parent_paths` 可以放入 `trace.json`。

### 7.2 C4 skeleton 残余定位耗时较长

本次 C4 skeleton 定位耗时约 279 秒，明显比 fine hierarchy 更重。

后续可考虑：

1. 对已定位的 TOC 节点减少 residual VLM。
2. 对 debug 模式增加更明确的 residual cap。
3. 对多个 coarse scope 并发定位/处理。
4. 复用 anatomy + skeleton cache 做快速迭代。

### 7.3 嵌入式 TOC 页 228 被跳过

本次 DOC_PROFILE 找到目录页 `[5, 6, 228]`，但 skeleton 阶段跳过了嵌入式目录区域 `[228]`。

这可能影响后续更细粒度 scope 的粗 hierarchy 完整性，需要单独评估：

```text
embedded_toc_region_outside_front_cluster
```

### 7.4 下一步测试建议

建议下一步直接测试：

```text
fine hierarchy -> PAGE-TAG
```

即跑到：

```text
--stop-at tag
```

重点检查：

1. `scopes/p225-301/page_tags.json`
2. 顶层 `page_tags.json`
3. `trace.json` 中 PAGE-TAG 是否只覆盖 `[[225, 301]]`
4. PAGE-TAG 是否能支撑后续 `chunks.json` 和 `doc_nav.json`

之后再开启图表资产提取，验证：

```text
assets.json
scopes/p225-301/assets.json
```

## 8. 当前涉及的主要代码文件

本轮 page-memory 相关核心改动集中在：

```text
apps/worker/app/services/page_memory/memory_service.py
apps/worker/app/services/page_memory/fine_hierarchy.py
apps/worker/app/services/page_memory/page_renderer.py
apps/worker/app/services/page_memory/page_assets.py
apps/worker/app/services/page_memory/node_assembler.py
apps/worker/app/services/document_agent/trace.py
apps/worker/app/services/document_agent/visual.py
apps/worker/scripts/debug_page_memory.py
```

其中 `apps/worker/scripts/debug_page_memory.py` 是当前测试入口。

