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
