# 长期稳定的 PDF 语义解析与章节树重建全流程计划

## 目标

构建一套长期稳定、泛用、健壮、可复用、准确的 PDF -> 语义 Markdown / 结构化 JSONL 全流程。

核心原则：

1. **一条主流程**：不长期维护两套互相竞争的解析算法。
2. **分层推理**：MinerU 负责版面/OCR 初解析，本项目负责语义结构重建。
3. **章节树优先**：Markdown 标题层级不再由单行局部规则直接决定，而由全局章节树反写。
4. **证据可解释**：每个标题、层级、父子关系都保留 evidence、confidence、decision_source。
5. **质量可回归**：任何规则升级都必须跑固定语料和质量门控，避免修一本坏十本。
6. **LLM 语义推理增强**：充分利用大模型的语义理解和推理能力处理低置信、冲突、跨页、无显式标题等复杂结构，但输出必须可缓存、可审计、可回退。
7. **保持简洁**：优先扩展现有流程和数据结构，只在确有必要时新增模块、CLI 或 profile。

## 简洁性约束

本次长期方案不追求“大而全”的架构，而追求一个小而硬的核心：

1. **只新增一个核心模块**
   - 首选新增 `section_tree.py`。
   - 暂不拆出复杂的 `document_model.py`、`profiles.py`，除非后续代码真的开始重复。

2. **优先复用现有产物**
   - 继续复用 `toc_tree.json`、`heading_candidates.jsonl`、`heading_decisions.jsonl`、`structured_blocks.jsonl`。
   - 新增 `section_tree.json` 作为唯一新的权威章节树产物。

3. **少加 CLI**
   - 第一阶段可以先把章节树构建接入 `mineru-build-structured-blocks`。
   - 只有需要单独调试时，再考虑 `mineru-build-section-tree`。

4. **少加规则**
   - 不为每本书写专门规则。
   - 只保留通用编号、TOC、正文顺序、同级一致性、负证据过滤。
   - 领域词表仅作为弱证据，不作为硬编码主逻辑。

5. **少用 LLM**
   - 主流程必须纯本地可跑。
   - 大模型作为增强层充分利用语义推理能力，但只接管局部不确定问题，不重写整本文档。
   - 所有大模型输出必须进入 JSON schema、动作白名单、缓存和质量门控。

6. **少改输出**
   - Phase 1 只新增 `section_tree.json`，不改变 `.semantic.md`。
   - 等树质量稳定后，再让 Markdown 层级由树反写。

## 最终架构

```text
docs/*.pdf
  -> MinerU batch parse
  -> raw merged Markdown / raw content blocks
  -> region classification
  -> heading candidate extraction
  -> heading repair decisions
  -> section tree reconstruction
  -> block-to-section assignment
  -> semantic Markdown / structured JSONL
  -> quality gates / regression reports
```

长期稳定后，Markdown 标题层级来源应是：

```text
section_tree.json -> tree_level -> Markdown # 层级
```

而不是：

```text
单个 MinerU text_level / 单行正则 -> Markdown # 层级
```

## 关键数据契约

### 1. `heading_candidates.jsonl`

职责：只回答“哪些块可能是标题”。

字段建议：

```json
{
  "candidate_id": "t1_p23_i12",
  "block_id": "中医内科学:1:23:12",
  "text": "第一节 感冒",
  "page": 23,
  "region": "body",
  "signals": {
    "numbered": true,
    "short_text": true,
    "toc_match": true,
    "layout_prominent": false
  },
  "candidate_score": 0.91
}
```

### 2. `heading_decisions.jsonl`

职责：只回答“候选标题本身如何修复”。

典型动作：

- `keep_heading`
- `promote_to_heading`
- `demote_to_paragraph`
- `split_heading`
- `merge_broken_heading`

不在此处做最终章节父子关系判断。

### 3. `section_tree.json`

职责：作为最终章节关系的权威来源。

字段建议：

```json
{
  "document": "中医内科学",
  "version": 1,
  "nodes": [
    {
      "section_id": "sec_000123",
      "title": "感冒",
      "normalized_key": "感冒",
      "level": 3,
      "parent_id": "sec_000087",
      "path": ["各论", "肺系病证", "感冒"],
      "start_page": 123,
      "end_page": 129,
      "start_block_id": "中医内科学:1:123:456",
      "end_block_id": "中医内科学:1:129:512",
      "confidence": 0.94,
      "evidence": [
        "toc_match",
        "numbering_pattern",
        "sibling_pattern_consistency"
      ]
    }
  ]
}
```

### 4. `structured_blocks.jsonl`

职责：每个块挂到最终章节树。

新增/稳定字段：

```json
{
  "block_id": "中医内科学:1:123:456",
  "block_type": "paragraph",
  "region": "body",
  "section_id": "sec_000123",
  "section_path": ["各论", "肺系病证", "感冒"],
  "tree_heading_level": 3,
  "recommended_for_rag": true
}
```

## 章节树重建算法

### 核心思路

使用“约束驱动的栈式树构建 + 全局修正”：

1. 按文档顺序扫描标题候选。
2. 为每个标题计算多个可能层级。
3. 基于证据为每个层级打分。
4. 在当前章节栈中选择代价最低的父节点。
5. 构建初始树。
6. 对同父同模式标题做全局归一。
7. 重新计算每个 section 的 start/end 范围。

### 大模型语义推理层

在本地规则和章节树构建之后，对以下局部问题调用大模型：

- 标题候选是否是真标题，还是正文句子、图表尾巴、参考文献、复习题。
- 某个标题应挂到哪个父章节，尤其是重复小节名如“病因病机”“治疗”“现代研究”。
- TOC backbone 与正文 heading 冲突时，判断以哪一方为准。
- 无 TOC 或正文标题缺失时，根据邻近上下文推断章节边界。
- 同页多个小节粘连时，判断块属于前一个小节还是后一个小节。

调用边界：

- 输入只给局部窗口：候选标题、上级路径、相邻 blocks、TOC path、当前本地决策。
- 输出只允许结构化 JSON：

```json
{
  "section_id": "sec_000123",
  "action": "keep|reparent|demote|split|merge",
  "parent_path": ["急性乳腺炎", "治疗"],
  "level": 3,
  "confidence": 0.86,
  "reason": "The heading is a treatment subsection under the disease chapter."
}
```

约束：

- 大模型不能直接改原文。
- 大模型不能批量重写全书结构。
- 低置信或 schema 不合法时回退本地树。
- 每次调用写入缓存和审计字段，例如 `decision_source=llm_section_reasoning`。

### 层级证据

强证据：

- TOC parent path 与页码范围匹配。
- 编号模式明确，如 `第X章`、`第X节`、`一、`、`（一）`、`1.`、`（1）`。
- 同一父节点下 siblings 的编号模式一致。

中证据：

- 字体/版面突出。
- 标题候选分数较高。
- 前后块是正文段落而不是目录页码。

弱证据：

- 短文本。
- 常见小节名，如“病因病机”“临床表现”“诊断”“治疗”。

负证据：

- 位于 `toc` region。
- 像目录页码条目。
- 像参考文献、CIP、页眉页脚、图表引用尾巴。
- 长句、问句、说明句。
- 数字比例或坐标轴 OCR 片段。

### 父子关系约束

必须满足：

- 父节点出现在子节点之前。
- 子节点页码不能早于父节点。
- 树不能成环。
- `toc` region 不参与正文章节树。
- 同一标题重复出现时，优先绑定最近的合法父节点和页码范围。

优先满足：

- TOC 路径一致。
- 编号层级一致。
- 同父 siblings 层级一致。
- 常见教材结构稳定，例如：
  - 篇 / 章 / 节
  - 疾病名 / 病因病机 / 临床表现 / 诊断 / 治疗
  - 方剂名 / 组成 / 功用 / 主治 / 方解

## 泛用性设计

不能把规则写死成“只适合中医教材”。需要分成三层：

1. **通用规则**
   - TOC/body/back matter 分区
   - 编号层级
   - 页眉页脚剔除
   - 短标题/长句判断
   - sibling consistency

2. **文档类型 profile**
   - textbook
   - monograph
   - guideline
   - paper
   - report

3. **领域词表 profile**
   - 中医教材小节名
   - 医学教材小节名
   - 论文 IMRaD 小节名

默认流程只依赖通用规则；领域 profile 只能加分，不能单独决定结构。

## 健壮性设计

1. **保守修复**
   - 低置信标题不强行升级。
   - LLM 输出只接受白名单动作。
   - 所有 split/merge 保留原文和来源块。

2. **失败可降级**
   - 没有 TOC 时仍可用编号树构建。
   - 编号混乱时回退到版面/上下文。
   - LLM 不可用时回退本地规则。

3. **可解释输出**
   - 每个 section node 记录 evidence。
   - 每个异常输出到 `section_tree_quality.json`。
   - 不静默吞掉低置信结果。

## 准确性与质量门控

新增质量检查：

- `section_tree_missing`
- `section_tree_cycle`
- `section_tree_orphan_node`
- `section_tree_level_jump`
- `section_tree_page_range_invalid`
- `toc_body_path_conflict`
- `same_pattern_sibling_tree_level_inconsistent`
- `body_block_without_section`
- `heading_not_in_section_tree`
- `toc_region_in_section_tree`

长期验收目标：

```text
mineru-heading-quality: 44 documents, 0 FAIL, 0 WARN
validate_outputs.py: 44 directories checked, 0 issue(s)
section_tree_quality: 0 FAIL, 0 WARN
```

## 复用性设计

模块建议保持克制：

```text
src/mineru_documents_markdown/section_tree.py
```

质量检查先并入现有 `heading_quality.py`，避免过早增加独立命令。只有当
tree 检查膨胀到难以维护时，再拆出：

```text
src/mineru_documents_markdown/section_tree_quality.py
```

可选 CLI：

```bash
.venv/bin/mineru-build-section-tree --document 中医内科学
.venv/bin/mineru-section-tree-quality --fail-on warn
.venv/bin/mineru-run-pipeline --section-tree
```

最终可并入现有：

```bash
.venv/bin/mineru-run-pipeline \
  --docs-dir docs \
  --output-dir output \
  --chunk-size 60 \
  --resubmit-failed \
  --repair-warn-with deepseek \
  --fail-on warn
```

## 分阶段实施

### Phase 1：建立章节树输出，不改变现有 Markdown

目标：

- 新增 `section_tree.py`。
- 生成 `section_tree.json`。
- 不改变当前 `.semantic.md` 输出。
- tree 质量检查优先并入 `heading_quality.py`。

价值：

- 可以和现有 `heading_level/section_path` 对照。
- 不破坏当前已经 0 FAIL / 0 WARN 的稳定基线。

验收：

```bash
.venv/bin/mineru-build-structured-blocks --heading-review-overrides output/docs_warn_deepseek_review.json
.venv/bin/mineru-heading-quality --fail-on warn
```

### Phase 2：让 `structured_blocks.jsonl` 挂接章节树

目标：

- 每个 body block 写入 `section_id`、`section_path`、`tree_heading_level`。
- 保留旧字段用于对照。
- 质量门控检查 tree 与旧字段差异。

验收：

- body 段落基本都能挂到合法 section。
- TOC、页眉页脚、参考文献不进入正文章节树。

### Phase 3：由章节树反写 Markdown 标题层级

目标：

- `.semantic.md` 的 `#` 层级来自 `section_tree.json`。
- 旧的局部 `heading_level` 变为候选层级，不再是最终层级。
- 命中章节树节点的正文标题使用 `tree_heading_level`；章节内部未进入树节点的小标题在所属树节点之下渲染，避免 TOC-backbone 文档被压平。

验收：

```bash
.venv/bin/mineru-run-pipeline --skip-parse --skip-review --fail-on warn
```

达到：

```text
44 documents, 0 FAIL, 0 WARN
44 directories checked, 0 issue(s)
```

### Phase 4：沉淀回归集和 profile

目标：

- 从 44 本中抽取代表性 fixture。
- 固化典型问题样本：
  - 目录污染
  - 断行标题
  - 同级漂移
  - CIP/参考文献误判
  - 图表引用尾巴
  - 问句型复习题
  - 无 TOC 文档
- 新增 `mineru-build-regression-fixtures`，从当前 `output/` 自动抽取紧凑结构样本，避免长期手工维护大体量 fixture。

产物：

```text
output/regression_fixtures/structure_regression_samples.json
output/regression_fixtures/structure_regression_samples.md
```

验收：

```bash
.venv/bin/python -m pytest
.venv/bin/mineru-run-pipeline --skip-parse --skip-review --fail-on warn
.venv/bin/mineru-build-regression-fixtures
```

## 风险与控制

风险：

- 一次性切换到章节树反写 Markdown，可能破坏当前稳定结果。

控制：

- 先生成 `section_tree.json` 旁路对照。
- 再把 blocks 挂树。
- 最后才由树反写 Markdown。
- 每阶段都跑全量质量门控。

风险：

- 领域 profile 过强，泛化变差。

控制：

- 通用规则优先。
- 领域词表只加分，不单独决定层级。
- 每条领域规则必须能在 quality report 中解释。

风险：

- LLM 不稳定。

控制：

- LLM 只处理低置信 WARN。
- 输出必须符合 JSON schema 和动作白名单。
- 缓存 review 结果。
- 主流程必须在没有 API Key 时可运行。

## 确认点

如果确认执行，建议按 Phase 1 开始：

1. 新增 `section_tree.py`。
2. 只生成旁路 `section_tree.json`，不改变当前 `.semantic.md`。
3. 将树质量检查并入现有 `heading_quality.py`。
4. 跑 44 本全量，比较章节树与现有 `section_path`。
5. 根据差异再决定 Phase 2 是否接管 `structured_blocks.jsonl`。
