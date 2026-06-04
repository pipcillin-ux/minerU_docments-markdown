# MinerU 文档批量解析为 Markdown

这个项目用于通过 MinerU API 将 PDF 文档解析为 Markdown。脚本会处理 MinerU 的页数限制、文件大小限制和限流策略：自动拆分 PDF、批量上传、轮询解析结果、下载结果包，并将每个文档重新合并成一个 Markdown 文件。

## 环境准备

进入项目目录，创建虚拟环境并安装依赖：

```bash
cd /Users/piperacillin/code/python_code/pdf
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

安装后会得到 4 个命令：

```text
mineru-batch-parse
mineru-validate-outputs
mineru-profile-documents
mineru-build-structured-blocks
```

在 `.env` 中配置 MinerU token：

```text
mineru_api_token=你的_TOKEN
```

脚本也支持通过 `--token` 参数或 `MINERU_TOKEN` 环境变量传入 token。

## 输入目录

把需要解析的 PDF 放到：

```text
docs/
```

默认情况下，脚本会处理 `docs/` 下所有 `*.pdf` 文件。

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
output/<文档名>/structured_blocks.jsonl
output/<文档名>/<文档名>.semantic.md
```

建议使用方式：

- `<文档名>.md`：保留 MinerU 原始合并版，用于溯源。
- `<文档名>.semantic.md`：规则修复后的结构版，用于 RAG、切块和知识抽取。
- `structured_blocks.jsonl`：机器可读结构化中间层，包含页码、分块、块类型、标题层级、章节路径、bbox、表格、图片等信息。

## 注意事项

- 项目采用标准 `src/` 布局，核心代码在 `src/mineru_documents_markdown/`。
- 顶层的 `mineru_batch_parse.py`、`validate_outputs.py`、`profile_documents.py`、`build_structured_blocks.py` 是兼容入口，旧命令仍可使用。
- 默认推荐使用本地上传模式，也就是只传本地 PDF 路径。
- `--url` 只适合单个公网 PDF URL。
- 真正解析时会把 PDF 内容上传到 MinerU。只想查看分批范围时，请使用 `--dry-run`。

## 贡献者

- piperacillin
- Codex
