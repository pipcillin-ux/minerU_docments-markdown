# Phase 5 大模型章节语义推理层计划

## 需求分析

当前主流程已经完成：

```text
MinerU 初解析
-> 语义重建
-> section_tree.json
-> structured_blocks.jsonl 挂树
-> .semantic.md 由章节树约束渲染
-> heading_quality 0 FAIL / 0 WARN
-> regression fixtures
```

下一阶段目标不是让大模型替代这条确定性主流程，而是在结构边界最容易漂移的位置引入“局部语义推理增强”：

- 重复小节名，例如“病因病机”“治疗”“现代研究”在不同疾病章节下反复出现。
- TOC backbone 与正文标题不完全一致，例如 TOC 有节点但正文标题缺失、断行、改写。
- 正文存在局部小标题，但没有进入 `section_tree.json`，需要判断是否应插入为子 section。
- 某个正文块处在跨页/同页多个标题之间，需要根据语义判断属于前一节还是后一节。
- 局部标题候选是否是正文句、列表项、图表尾巴、参考文献。

约束：

- 主流程必须在无 API Key 时仍完整可跑。
- 大模型只处理局部窗口，不接收或重写整本文档。
- 大模型输出必须是 JSON schema + 动作白名单。
- 所有结果必须可缓存、可审计、可回退。
- 第一阶段默认只生成候选与 review，不直接改写最终输出；自动应用要等回归集稳定后再打开。

## 技术路径

### 1. 新增章节推理候选

新增模块：

```text
src/mineru_documents_markdown/section_reasoning.py
```

新增 CLI：

```text
mineru-section-reasoning
```

候选文件：

```text
output/<文档名>/section_reasoning_candidates.jsonl
output/<文档名>/section_reasoning_decisions.jsonl
output/<文档名>/section_reasoning_report.md
```

候选类型：

- `local_heading_under_tree_node`
  - 正文 heading 的 `text` 不是当前 `tree_section_path` 最后一项。
  - 但它有局部 `heading_level`，可能应作为当前 section 的子节点。
- `toc_node_unanchored_or_weak`
  - `section_tree` 节点标题和 `source_block_id` 对应文本不匹配，说明 TOC 节点可能只是按页兜底锚定。
- `repeated_title_boundary`
  - 同名小节在同一文档多次出现，且邻近正文包含不同疾病/章节语义。
- `cross_page_boundary`
  - section 的 `start_page/end_page` 跨页，且边界附近存在多个候选标题。
- `low_confidence_tree_node`
  - `section_tree` 节点 confidence 低于阈值，或 evidence 只有弱证据。

### 2. 大模型输入窗口

每个候选只发送局部上下文：

```json
{
  "document": "外科专病中医临床诊治",
  "candidate_type": "local_heading_under_tree_node",
  "current_block": {
    "block_id": "...",
    "page": 26,
    "text": "(一) 中医",
    "heading_level": 3,
    "tree_section_path": ["急性乳腺炎", "病因病机"]
  },
  "current_section": {
    "section_id": "sec_000002",
    "path": ["急性乳腺炎", "病因病机"],
    "level": 2
  },
  "nearby_blocks": [],
  "nearby_sections": [],
  "toc_context": []
}
```

### 3. 大模型输出 schema

动作白名单：

- `keep`
- `insert_child_section`
- `reparent_block`
- `merge_with_previous_section`
- `demote_to_paragraph`
- `uncertain`

输出：

```json
{
  "candidate_id": "外科专病...:1:26:123",
  "action": "insert_child_section",
  "target_parent_id": "sec_000002",
  "title": "(一) 中医",
  "level": 3,
  "confidence": 0.86,
  "reason": "This is a subsection under 病因病机, not prose."
}
```

非法 schema、低置信度、缺失候选 ID 时回退为 `uncertain`。

### 4. 缓存与审计

复用现有 `llm_heading_assist.py` 的 OpenAI-compatible 调用方式：

- `DEEPSEEK_API_KEY` / `OPENAI_API_KEY`
- `DEEPSEEK_BASE_URL`
- `DEEPSEEK_MODEL`
- JSON response format
- payload hash cache

新增缓存目录：

```text
output/<文档名>/.section_reasoning_cache/
```

每条决策保留：

- `candidate_id`
- `decision_source = llm_section_reasoning`
- `input_hash`
- `action`
- `confidence`
- `reason`
- `fallback_action`

### 5. 应用策略

Phase 5A 只做候选 + review：

```bash
.venv/bin/mineru-section-reasoning --mode collect
.venv/bin/mineru-section-reasoning --mode review --limit 20
```

本阶段实现范围：

- `collect`：从现有 `structured_blocks.jsonl`、`section_tree.json`、`toc_tree.json` 生成候选。
- `report`：生成每本文档的 Markdown 审计报告。
- `review`：可选调用 DeepSeek/OpenAI-compatible 接口，写入 schema 校验后的决策。
- 不实现 `apply`，不改写主 `section_tree.json`、`structured_blocks.jsonl`、`.semantic.md`。

Phase 5A 状态：已实现并提交。

Phase 5B 允许受控应用：

```bash
.venv/bin/mineru-section-reasoning --mode apply --min-confidence 0.82
```

Phase 5B 状态：已实现。当前只自动应用超过置信阈值的 `insert_child_section` 决策；其他动作仍保留在审计文件中，不自动改写。

应用时只写旁路产物：

- `section_tree.reasoned.json`
- `structured_blocks.reasoned.jsonl`
- `<文档名>.semantic.reasoned.md`
- `section_reasoning_apply_report.md`

不会覆盖主输出，直到新的质量门禁和回归集稳定。

## 验收

Phase 5A：

```bash
.venv/bin/mineru-section-reasoning --mode collect --limit 200
.venv/bin/mineru-section-reasoning --mode report
.venv/bin/mineru-heading-quality --fail-on warn
.venv/bin/mineru-build-regression-fixtures
```

通过标准：

- 无 API Key 时 collect/report 可运行。
- 有 API Key 时 review 可缓存、可复跑。
- review 输出全部符合 schema，非法响应回退 `uncertain`。
- 现有主流程仍保持 `0 FAIL / 0 WARN`。

Phase 5B：

```bash
.venv/bin/mineru-section-reasoning --mode apply --min-confidence 0.82
.venv/bin/mineru-heading-quality --fail-on warn
```

通过标准：

- 只生成 `.reasoned.*` 旁路产物。
- 应用后不得降低现有 heading quality。
- 回归样本报告能体现 LLM 决策来源和理由。
- 已用“外科专病中医临床诊治 第3版”的一条高置信 `insert_child_section` 决策验证：原树 347 节点，reasoned 树 348 节点，新增 `llm_sec_000001`，Markdown 渲染为 `### (一)中医`。

## Phase 5C：高质量采纳到主输出计划

状态：已实现。

### 需求分析

当前 `mineru-run-pipeline` 已覆盖确定性主流程：

```text
parse
-> validate
-> profile
-> build semantic Markdown
-> heading quality
-> optional DeepSeek WARN review/rebuild
-> final heading quality
-> final validation
```

章节语义推理层已经具备独立 CLI：

```bash
.venv/bin/mineru-section-reasoning --mode collect
.venv/bin/mineru-section-reasoning --mode review
.venv/bin/mineru-section-reasoning --mode apply
```

下一步目标不是长期停留在旁路文件，而是把大模型高质量语义推理结果安全采纳到主输出：

- 默认不调用 LLM，不改变现有 `mineru-run-pipeline` 行为。
- 可选开启 collect/report，用于生成章节结构审计。
- 可选开启 review，需要 API Key，继续使用缓存和 schema 校验。
- 可选开启 apply，生成 `.reasoned.*` 候选产物，用于审计和调试。
- 可选开启 adopt，把通过采纳门的高置信候选晋升为主输出。
- 单文档运行时尊重 `--document` / `--pdf` 推导出的文档名。
- 全量运行时只对存在高质量决策且通过校验的文档改写主输出。
- 主输出采纳不是只看 LLM 自评置信度；必须经过确定性结构校验、质量门和失败回滚。

### 技术路径

#### 1. 章节推理 CLI 增加采纳目标

在 `src/mineru_documents_markdown/section_reasoning.py` 中新增：

```text
--mode adopt
--target {sidecar,main}
--adoption-backup
```

建议语义：

- `apply`：继续生成 `.reasoned.*` 候选产物，默认不覆盖主输出。
- `adopt`：先生成 reasoned 候选，再通过采纳门，最后写回主输出。
- `--target main`：作为 `adopt` 的显式写主输出模式；没有该显式参数时不改主输出。

#### 2. 采纳门

只有同时满足以下条件，才允许写入主输出：

**决策级条件**

- `decision_source == llm_section_reasoning`。
- `action` 在当前允许自动采纳白名单内。第一版只允许 `insert_child_section`。
- `confidence >= --min-confidence`，默认不低于 `0.86`。
- `candidate_id` 完全匹配原候选，候选与当前块仍一致。
- `target_parent_id` 存在，父节点层级合法，新增层级必须在父节点之下。

**结构级条件**

- 新 section 的 `source_block_id` 必须存在于 `structured_blocks.jsonl`。
- source block 必须是 `body` 区域、`include_in_semantic != false`。
- 新节点不能和同父节点已有节点重复。
- 重算后所有 node range 必须满足：
  - `start_block_id/end_block_id` 均存在。
  - `document_order` 与 block 顺序一致。
  - child range 不越出 parent range。
  - 无同级范围反向或明显交叉。
- 目录区、front matter、back matter 不能因为采纳而进入正文树。

**输出级条件**

- 采纳前后原文内容不被 LLM 改写，只允许结构字段、章节树和 Markdown 标题层级变化。
- 新 `.semantic.md` 必须能由采纳后的 `structured_blocks.jsonl` 和 `section_tree.json` 重新渲染。
- 写主输出后必须重新运行 `mineru-heading-quality --fail-on warn`，不允许新增 FAIL/WARN。
- 失败时恢复原 `section_tree.json`、`structured_blocks.jsonl`、`<文档名>.semantic.md`。

#### 3. 主输出写入策略

采用“候选生成 -> 原子晋升 -> 失败回滚”：

```text
base main outputs
-> build reasoned candidate in memory / sidecar
-> run adoption checks
-> snapshot original main files
-> write main section_tree.json / structured_blocks.jsonl / semantic.md
-> run heading quality
-> pass: keep main outputs and write adoption report
-> fail: restore snapshots and mark rejected
```

采纳报告：

```text
output/<文档名>/section_reasoning_adoption_report.md
```

报告记录：

- 被采纳的 decision 和 reason。
- 被拒绝的 decision 和拒绝原因。
- 采纳前后 node_count、semantic heading 变化。
- 质量门结果。
- 是否发生回滚。

#### 4. 接入统一流水线

在 `src/mineru_documents_markdown/run_pipeline.py` 中新增参数：

```text
--section-reasoning {none,collect,review,apply,adopt}
--section-reasoning-limit <int>
--section-reasoning-min-confidence <float>
--skip-section-reasoning
```

建议语义：

- `none`：默认值，完全跳过章节语义推理。
- `collect`：在 final heading quality 通过后运行 `collect` + `report`。
- `review`：先运行 `collect`，再运行 `review`，最后刷新 `report`。
- `apply`：先确保有候选；若存在决策则运行 `apply`，生成 reasoned 旁路产物。
- `adopt`：先确保有候选和决策；运行 `adopt --target main`，通过采纳门后写主输出。

命令阶段建议顺序：

```text
final heading quality
-> optional section reasoning collect/review/apply
-> if adopt: re-run final heading quality after main output promotion
-> final output validation
```

原因：

- 章节推理依赖稳定的 `structured_blocks.jsonl`、`section_tree.json`、`toc_tree.json`。
- apply 只写候选产物，不影响 final validation 对主输出的判断。
- adopt 会改主输出，因此必须在采纳后再次跑 heading quality 和 validation。
- review/apply/adopt 出错时应作为独立阶段失败，便于定位。

README 更新：

- 在正式命令示例中增加可选 section reasoning 参数。
- 明确默认正式命令仍是确定性主流程。
- 给出带 DeepSeek 复核、reasoned 候选生成和主输出采纳的增强命令。

### 验收

实现后验证：

```bash
.venv/bin/python -m py_compile src/mineru_documents_markdown/run_pipeline.py
.venv/bin/mineru-run-pipeline --help
.venv/bin/mineru-run-pipeline --skip-parse --skip-review --section-reasoning collect --fail-on warn
.venv/bin/mineru-run-pipeline --skip-parse --skip-review --section-reasoning apply --section-reasoning-min-confidence 0.82 --fail-on warn
.venv/bin/mineru-run-pipeline --skip-parse --skip-review --section-reasoning adopt --section-reasoning-min-confidence 0.86 --fail-on warn
.venv/bin/mineru-heading-quality --fail-on warn
git diff --check
```

通过标准：

- 默认 `mineru-run-pipeline --skip-parse --skip-review` 行为不变。
- `collect` 只生成候选和报告，不调用 LLM。
- `apply` 只生成 `.reasoned.*` 旁路产物。
- `adopt` 只采纳通过采纳门的高置信 LLM 决策，并写入主输出。
- 若采纳后质量门失败，主输出自动回滚。
- 全量 heading quality 保持 `0 FAIL / 0 WARN`。

实现备注：

- `adopt` 已接入 `mineru-run-pipeline --section-reasoning adopt`，采纳后会再次运行 final heading quality。
- `section_reasoning adopt --target main` 会写主 `section_tree.json`、`structured_blocks.jsonl`、`<文档名>.semantic.md`，并生成 `section_reasoning_adoption_report.md`。
- 采纳门会比较 base 与 reasoned 的结构问题，只阻断新增结构问题，避免历史基线问题误杀。
- 修复了 `attach_section_tree` 中 page fallback 抢过 index range 的问题：存在精确 index 匹配时不再使用页码兜底匹配。
- LLM 插入的小节遇到后续同级/更高级正文 heading 时会提前截断 range，避免 `(一)` 小节吞掉 `(二)` 或下一节。

## Phase 5D：全库章节推理汇总计划

状态：已实现。

### 需求分析

在高置信 LLM 决策可以进入主输出之后，下一步问题变成“哪些文档值得继续调用
DeepSeek、哪些文档已有高质量决策但还没采纳、哪些文档已经进入主输出”。如果
只看每本书目录下的局部报告，很难判断全库优先级，也容易重复花费 LLM 调用。

因此新增一个只读的 corpus summary 层：

- 不调用大模型。
- 不改写 `section_tree.json`、`structured_blocks.jsonl`、`.semantic.md`。
- 读取每本文档的候选、决策、旁路产物和采纳报告。
- 输出全库级 CSV 和 Markdown 汇总，作为批量 review/adopt 的操作面板。
- 统一流水线启用 `--section-reasoning collect|review|apply|adopt` 时，也会在对应阶段之后自动刷新全库 summary。
- `review --limit` 默认增量推进：跳过已有决策的候选，合并写回新决策，避免重复复核同一批候选。
- summary 的 `adoption_ready` 已纳入结构门校验；adopt 对没有新增可采纳决策的文档保持幂等。
- `review --review-jobs N` 支持受控并发，只并发 LLM API 调用，候选选择和决策写回仍串行。

### 技术路径

新增模式：

```bash
.venv/bin/mineru-section-reasoning --mode summary --min-confidence 0.86
```

输出：

```text
output/section_reasoning_summary.csv
output/section_reasoning_summary.md
```

汇总维度：

- 每本文档的候选数量和候选类型分布。
- 已有 LLM 决策数量和 action 分布。
- 达到置信阈值的 `insert_child_section` 决策数量。
- 通过当前采纳检查、但尚未进入主输出的决策数量。
- 主 `section_tree.json` 中已采纳的 `llm_section_reasoning` 节点数量。
- `section_reasoning_adoption_report.md` 中记录的采纳状态。
- 待复核队列、待采纳队列、已采纳队列和高置信但不可采纳队列。

### 验收

已验证：

```bash
.venv/bin/python -m py_compile src/mineru_documents_markdown/section_reasoning.py
.venv/bin/python -m py_compile src/mineru_documents_markdown/run_pipeline.py
.venv/bin/mineru-section-reasoning --help
.venv/bin/mineru-run-pipeline --help
.venv/bin/mineru-section-reasoning --mode collect
.venv/bin/mineru-section-reasoning --mode summary --min-confidence 0.86
```

当前全库结果：

- 44 本文档。
- 1641 个章节推理候选。
- 1 条已有 LLM 决策。
- 1 个主输出中已采纳的 LLM 推理节点。
- 其余文档进入 review queue，适合后续分批调用 DeepSeek 复核。
