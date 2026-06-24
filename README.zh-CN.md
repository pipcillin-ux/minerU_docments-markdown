# MinerU 文档批量解析为 Markdown

这个项目用于通过 MinerU API 将 PDF 文档解析为 Markdown。脚本会处理 MinerU 的页数限制、文件大小限制和限流策略：自动拆分 PDF、批量上传、轮询解析结果、下载结果包，并将每个文档重新合并成一个 Markdown 文件。

## 环境准备

进入项目目录，创建虚拟环境并安装依赖：

```bash
cd /Users/piperacillin/code/python_code/pdf
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

安装后会得到 8 个命令：

```text
mineru-batch-parse
mineru-validate-outputs
mineru-profile-documents
mineru-build-structured-blocks
mineru-heading-quality
mineru-build-regression-fixtures
mineru-section-reasoning
mineru-run-pipeline
```

在 `.env` 中配置 MinerU token：

```text
mineru_api_token=你的_TOKEN
```

进程环境变量 `MINERU_TOKEN` 的优先级高于 `.env`。为避免 token 出现在
进程列表中，命令行不再接受 token 参数。

## 输入目录

把需要解析的 PDF 放到：

```text
docs/
```

默认情况下，脚本会处理 `docs/` 下所有 `*.pdf` 文件。

## 一条命令跑完全流程

正式推荐命令会从 `docs/` 读取 PDF，在独立工作目录中完成解析、诊断、
语义重建、WARN 修复和最终校验；只有验证通过的结果才会发布到 `output/`：

```bash
.venv/bin/mineru-run-pipeline \
  --docs-dir docs \
  --output-dir output \
  --chunk-size 60 \
  --resubmit-failed \
  --repair-warn-with deepseek \
  --fail-on warn
```

流水线顺序：

```text
解析 -> 诊断 -> 重建 -> 修复 -> 验证 -> 发布
```

默认工作目录是与 `output/` 同级的 `.output.pipeline-work`。macOS 下，
`--skip-parse` 会优先使用 APFS copy-on-write 克隆初始化工作区，不会为
大型语料再做一份完整物理复制。诊断报告、复核文件、章节推理旁路产物和
质量报告等所有子命令输出都只写入工作区。

如果任一阶段失败或流程被中断，正式 `output/` 保持不变，工作目录会保留。
重新运行同一条命令即可续跑；如果希望丢弃失败现场并重新初始化，增加
`--fresh-work`。全部质量门通过后，工作目录才会以可回滚的目录交换方式发布。

`--fail-on warn` 用于把最终目标收紧到 `0 FAIL / 0 WARN`；如果只希望
阻断明确错误、允许保留 WARN，可改成 `--fail-on fail`。DeepSeek WARN
修复默认需要 `DEEPSEEK_API_KEY` 或 `.env` 中的 `deepseek_api_key`。

如果 `output/` 已经有解析结果，`--skip-parse` 会先把正式快照克隆到工作区，
然后复跑诊断、语义重建、修复、验证和发布：

```bash
.venv/bin/mineru-run-pipeline \
  --docs-dir docs \
  --output-dir output \
  --skip-parse \
  --repair-warn-with deepseek \
  --fail-on warn
```

如果要复用已有的 review/override 文件：

```bash
.venv/bin/mineru-run-pipeline \
  --skip-parse \
  --heading-review-overrides output/docs_warn_deepseek_review.json \
  --skip-review
```

如果不想调用 DeepSeek/OpenAI-compatible 复核，只使用本地规则：

```bash
.venv/bin/mineru-run-pipeline \
  --docs-dir docs \
  --output-dir output \
  --skip-parse \
  --repair-warn-with none \
  --fail-on fail
```

如果要把已经复核过的高置信章节推理决策采纳到主输出：

```bash
.venv/bin/mineru-run-pipeline \
  --docs-dir docs \
  --output-dir output \
  --skip-parse \
  --heading-review-overrides output/docs_warn_deepseek_review.json \
  --skip-review \
  --section-reasoning adopt \
  --section-reasoning-min-confidence 0.86 \
  --fail-on warn
```

`adopt` 会先生成 reasoned 候选，再只晋升通过确定性结构校验和采纳后标题质量门的决策。如果质量门失败，会恢复原主输出的 `section_tree.json`、`structured_blocks.jsonl` 和 `<文档名>.semantic.md`。
重建时会先应用已有的标题复核覆盖文件，因此无需再次调用 API，也可以通过章节采纳前的质量门。
只要启用 `--section-reasoning collect|review|apply|adopt`，流水线也会同步刷新
`output/section_reasoning_summary.csv` 和
`output/section_reasoning_summary.md`。

DeepSeek 复核会从环境变量或 `.env` 读取 `DEEPSEEK_API_KEY` /
`deepseek_api_key`。复核文件先写入工作区，发布成功后才出现在：

```text
output/heading_warn_deepseek_review.json
output/heading_warn_deepseek_review.md
```

## 批量解析所有 PDF

运行：

```bash
.venv/bin/mineru-batch-parse
```

脚本会自动完成：

1. 统计 PDF 页数。
2. 按最多 200 页拆分 PDF。
3. 保证每个上传分块小于 MinerU 的 200 MB 单文件限制。
4. 通过 MinerU 签名上传 URL 上传分块。
5. 轮询批量解析结果。
6. 下载每个分块的结果 zip。
7. 将分块 Markdown 合并为每个 PDF 对应的一个 Markdown 文件。
8. 将图片引用改写为本地 `assets/part_xxx/` 路径。

## 预检查

只查看页数和分批范围，不上传文件：

```bash
.venv/bin/mineru-batch-parse --dry-run
```

## 解析单个 PDF

```bash
.venv/bin/mineru-batch-parse \
  --pdf docs/example.pdf \
  --out output/example
```

不传 `--pdf` 时，`--out` 是总输出目录。

传入 `--pdf` 时，`--out` 是该单个 PDF 的输出目录。
输出目录和 Markdown 文件名会完整保留 PDF stem；批量模式会在写入前检查
大小写不敏感的重名，发现冲突立即停止。

## 断点续跑和失败重试

每个输出目录都会生成 `tasks.json`，用于记录分块任务状态，因此中断后可以继续运行。

只提交缺失任务，不等待解析完成：

```bash
.venv/bin/mineru-batch-parse --submit-only
```

重试失败任务：

```bash
.venv/bin/mineru-batch-parse --resubmit-failed
```

## MinerU 限流策略

脚本默认遵守以下 MinerU 限制：

- 提交任务/批量上传接口：50 个文件/分钟。
- 获取任务结果接口：1000 次/分钟。
- 单用户每日最多上传 5000 个文件。
- HTML 文件每日最多 100 个，本项目不使用 HTML 上传。

默认参数：

```bash
--submit-files-per-minute 50
--result-requests-per-minute 1000
--daily-upload-file-limit 5000
--chunk-size 200
--max-upload-mb 200
```

如果某个分块仍超过 200 MB，可以减小页数分块大小后重试：

```bash
.venv/bin/mineru-batch-parse --chunk-size 100 --resubmit-failed
```

## 输出结构

每个 PDF 的结果会输出到：

```text
output/<文档名>/
  <文档名>.md
  tasks.json
  chunks/
  parts/
  assets/
```

文件名会自动清理：

- 去掉开头数字书号。
- 去掉结尾数字书号。
- 去掉类似 `(1)` 的重复下载编号。
- 合并连续空格。

示例：

```text
docs/骨伤科专病中医临床诊治_13773573.pdf
```

会输出为：

```text
output/骨伤科专病中医临床诊治/骨伤科专病中医临床诊治.md
```

## 校验结果

运行：

```bash
.venv/bin/mineru-validate-outputs
```

校验脚本会逐个输出目录检查：

- 所有任务是否都是 `done`。
- 分块 PDF 页数是否和 `page_range` 一致。
- 合并后的 Markdown 是否包含每个分块 Markdown。
- 图片引用是否都能找到对应本地文件。

正常结果示例：

```text
Validation complete: 16 directories checked, 0 issue(s).
```

## 结构偏移诊断与语义重建

MinerU 输出的 Markdown 是候选解析结果，不应直接等同于原文语义结构。复杂 PDF 可能出现标题层级扁平、目录污染正文、页眉页脚混入、表格降级为文本、图片型表格遗漏等问题。

生成文档画像和结构诊断：

```bash
.venv/bin/mineru-profile-documents
```

该命令会生成：

```text
output/document_profiles_summary.csv
output/quality_report.md
output/<文档名>/document_profile.json
output/<文档名>/structure_diagnostics.json
output/<文档名>/quality_report.md
```

生成结构化中间层和语义修复版 Markdown：

```bash
.venv/bin/mineru-build-structured-blocks
```

该命令会生成：

```text
output/<文档名>/toc_tree.json
output/<文档名>/section_tree.json
output/<文档名>/heading_candidates.jsonl
output/<文档名>/heading_decisions.jsonl
output/<文档名>/heading_diagnostics.json
output/<文档名>/structured_blocks.jsonl
output/<文档名>/<文档名>.semantic.md
```

建议使用方式：

- `<文档名>.md`：保留 MinerU 原始合并版，用于溯源。
- `<文档名>.semantic.md`：结构修复后的全文语义版，默认保留前置页、正文、参考文献和附录，只去掉页眉、页脚、页码等重复噪声。
- `structured_blocks.jsonl`：机器可读结构化中间层，包含页码、分块、块类型、标题层级、旧版章节路径、章节树字段、bbox、表格、图片、标题决策审计等信息。`section_id`、`tree_section_path`、`tree_heading_level`、`tree_section_source`、`tree_section_confidence` 会把正文块挂到重建后的章节树；`recommended_for_rag` 标记更适合进入 RAG 的正文块。
- `toc_tree.json`：从目录页抽取的目录树，包含父路径和页码提示。
- `section_tree.json`：正文章节树产物，优先使用正文标题恢复父子关系；当正文标题不足时，使用 TOC backbone 兜底。它已用于给每个正文块回填稳定章节路径，并用于约束语义 Markdown 的正文标题层级。
- `heading_candidates.jsonl`：本地标题候选，包含文本、版面和上下文信号。
- `heading_decisions.jsonl`：标题修复决策记录，支持 `keep_heading`、`promote_to_heading`、`demote_to_paragraph`、`split_heading`。
- `heading_diagnostics.json`：语义标题结构质检指标。

### 当前结构修复规则

近期优化后的语义重建会显式处理结构偏移问题：

- 目录边界：按 item 级别识别 `front_matter`、`toc`、`body`、`back_matter`，目录区渲染为普通 `**目录**` 区块，不再把目录条目写成 Markdown 标题；目录块默认不进入 RAG 正文分块。
- 标题层级：优先使用 `toc_tree.json` 和编号模式推断层级，正文同模式兄弟标题会做一致性检查，避免同一层级一会儿变成一级标题、一会儿变成三级标题。
- 章节树：生成 `section_tree.json` 作为长期稳定的正文父子关系层，并将树归属字段回填到 `structured_blocks.jsonl`。语义 Markdown 的正文标题层级由章节树约束：命中章节节点本身的标题使用节点层级，章节内部的局部小标题会落在所属树节点之下。
- 断行标题：支持保守合并被拆断的章节标题，并把“标题 + 正文句子”“标题 + 图表引用尾巴”等粘连文本拆回标题和正文。
- 非标题降级：目录索引、CIP/编目行、参考文献条目、坐标轴/OCR 数字串、图表说明尾巴、复习题问句、长编号正文句/列表项等不再参与正文标题层级判断。
- 质量门控：`mineru-heading-quality` 会检查 TOC 泄漏、目录项进入正文大纲、标题层级跳跃、同模式兄弟标题不一致等问题；本仓库当前全量输出目标是 `0 FAIL / 0 WARN`。

如果只想生成正文范围的结构版 Markdown，可以使用：

```bash
.venv/bin/mineru-build-structured-blocks --semantic-scope body
```

标题识别策略：

```bash
# 只用本地规则，默认模式，可复现。
.venv/bin/mineru-build-structured-blocks --heading-strategy rule

# 所有标题候选都交给 OpenAI-compatible 大模型判断。
.venv/bin/mineru-build-structured-blocks --heading-strategy llm

# 高置信候选用规则，低置信候选交给大模型，推荐增强模式。
.venv/bin/mineru-build-structured-blocks --heading-strategy hybrid
```

DeepSeek 可以作为可选的大模型辅助层：

```bash
export DEEPSEEK_API_KEY="..."
export DEEPSEEK_BASE_URL="https://api.deepseek.com"
export DEEPSEEK_MODEL="deepseek-chat"
.venv/bin/mineru-build-structured-blocks --heading-strategy hybrid
```

大模型只会收到标题候选和少量局部上下文，不会被要求重写整本文档。如果没有配置 API Key、请求超时或返回 JSON 不合法，程序会回退到本地规则决策。

可选的章节关系大模型语义推理层用于处理重复小节名、TOC 与正文冲突、缺失显式标题等局部树结构问题。该层只接收小窗口上下文，输出 schema 校验后的动作，结果可缓存、可审计、可回退；不会直接改写原文，也不会替代确定性主流程。

不调用大模型，仅收集章节语义推理候选：

```bash
.venv/bin/mineru-section-reasoning --mode collect --limit 200
.venv/bin/mineru-section-reasoning --mode report
.venv/bin/mineru-section-reasoning --mode summary --min-confidence 0.86
```

`summary` 是只读汇总模式，不调用大模型，也不改主输出。它会把全库的复核
和采纳状态整理成：

```text
output/section_reasoning_summary.csv
output/section_reasoning_summary.md
```

汇总报告会列出候选数量、已复核决策、高置信插入决策、通过结构门的当前可采纳决策、
历史孤立审计决策、主输出中已经采纳的 LLM 推理节点、仍待复核的文档和已经进入主输出的文档。

使用 DeepSeek/OpenAI-compatible JSON 响应复核候选：

```bash
.venv/bin/mineru-section-reasoning --mode review --limit 80 --review-jobs 4
```

review 默认是增量模式：会跳过已经写入 `section_reasoning_decisions.jsonl`
的候选，把新决策合并回原决策文件；只有传入 `--force` 时才会重新复核已缓存候选。
`--review-jobs` 只并发 API 复核调用；候选选择和决策文件写入仍串行完成，避免重复候选或并发写入竞争。

collect/review 只写旁路审计文件：

```text
output/<文档名>/section_reasoning_candidates.jsonl
output/<文档名>/section_reasoning_decisions.jsonl
output/<文档名>/section_reasoning_report.md
```

将高置信复核决策应用到 reasoned 旁路产物：

```bash
.venv/bin/mineru-section-reasoning --mode apply --min-confidence 0.86
```

apply 当前保持保守：只落地超过置信阈值的 `insert_child_section` 决策，然后基于原始主产物重新挂载正文块。候选收集会跳过已经锚定任一 section node 的正文标题；apply 也会用 `source_already_section_node` 拒绝历史遗留的重复锚点决策。

章节范围采用局部树更新：新增节点由后续同级标题和父节点有效包络共同约束，只允许扩展它的父节点与祖先链。已经通过校验的原范围可以放宽 TOC 推导出的过早边界，但不能越过任一祖先；没有新增节点时，原始 range 完全不变。apply 不会覆盖 `section_tree.json`、`structured_blocks.jsonl` 或 `<文档名>.semantic.md`。

```text
output/<文档名>/section_tree.reasoned.json
output/<文档名>/structured_blocks.reasoned.jsonl
output/<文档名>/<文档名>.semantic.reasoned.md
output/<文档名>/section_reasoning_apply_report.md
```

将高置信决策采纳到主输出：

```bash
.venv/bin/mineru-section-reasoning \
  --mode adopt \
  --target main \
  --min-confidence 0.86
```

adopt 当前只自动晋升 `llm_section_reasoning` 来源的 `insert_child_section` 决策。它会生成 `section_reasoning_adoption_report.md`，检查原文文本未被改写、没有新增章节范围缺陷，并在采纳后出现 FAIL/WARN 时自动回滚。
对没有新增可采纳决策的文档，重复运行 adopt 是幂等的，不会把已处理文档误判为失败。

运行父子范围专项回归测试：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

大文档建议控制单次请求大小：

```bash
.venv/bin/mineru-build-structured-blocks \
  --heading-strategy hybrid \
  --llm-confidence-threshold 0.6 \
  --llm-batch-size 5
```

运行固化后的标题质量检查：

```bash
.venv/bin/mineru-heading-quality
```

质量状态含义：

- `FAIL`：发现明确结构错误，例如目录项仍然粘连、无语义标题、标题决策文件损坏，固定流水线应阻断。
- `WARN`：发现需要抽查的结构风险，例如长句式标题、同级标题层级不一致、标题层级跳跃，可交给 DeepSeek/hybrid 模式或人工复核。
- `INFO`：提示性信息，例如目录节点未在正文标题中匹配，通常不阻断流程。

该命令会生成：

```text
output/heading_quality_summary.csv
output/<文档名>/heading_quality.json
output/<文档名>/heading_quality.md
```

更严格的固定流水线建议直接使用正式命令：

```bash
.venv/bin/mineru-run-pipeline \
  --skip-parse \
  --heading-strategy hybrid \
  --llm-confidence-threshold 0.6 \
  --llm-batch-size 5 \
  --fail-on fail
```

从当前语料生成紧凑回归样本：

```bash
.venv/bin/mineru-build-regression-fixtures
```

该命令会输出：

```text
output/regression_fixtures/structure_regression_samples.json
output/regression_fixtures/structure_regression_samples.md
```

样本覆盖目录/正文边界、TOC-backbone 与正文标题章节树、正文块挂树、标题拆分/合并、长编号正文句降级、Markdown 标题层级由树反写等典型结构问题。

## 注意事项

- 项目采用标准 `src/` 布局，核心代码在 `src/mineru_documents_markdown/`。
- 顶层的 `mineru_batch_parse.py`、`validate_outputs.py`、`profile_documents.py`、`build_structured_blocks.py` 是兼容入口，旧命令仍可使用。
- 默认推荐使用本地上传模式，也就是只传本地 PDF 路径。
- `--url` 只适合单个公网 PDF URL。
- 真正解析时会把 PDF 内容上传到 MinerU。只想查看分批范围时，请使用 `--dry-run`。

## 贡献者

- piperacillin
- Codex
