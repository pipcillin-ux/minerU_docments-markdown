# 事务式 PDF 全流程发布计划

## 需求分析

正式命令当前会让解析、诊断、重建、修复和验证阶段直接写入
`--output-dir`。任一中间阶段失败时，正式输出目录会混入未修复或未验证的
半成品。

目标流程调整为：

```text
解析 -> 诊断 -> 重建 -> 修复 -> 验证 -> 发布
```

其中前五个阶段全部在独立工作目录中执行。只有所有质量门通过后，才更新正式
输出；失败时旧正式输出保持不变，工作目录保留用于排查和断点续跑。

## 技术路径

### 1. 稳定工作目录

- 新增 `--work-dir`，默认使用正式输出目录同级的
  `.<output-name>.pipeline-work`。
- 正常解析直接写工作目录，MinerU 的 `tasks.json`、分块和下载缓存均可续跑。
- `--skip-parse` 且工作目录不存在时，从当前正式输出克隆一个工作快照。
- 工作目录已存在时默认继续使用；新增 `--fresh-work` 可显式丢弃并重新初始化。
- 正式输出目录与工作目录禁止相同或互相嵌套。

### 2. 阶段隔离

流水线内部所有子命令的 `--output-dir` / `--out` 都指向工作目录：

1. MinerU parse
2. validate parse outputs
3. profile / structure diagnostics
4. semantic rebuild
5. WARN review and repair rebuild
6. heading quality
7. section reasoning collect/review/apply/adopt
8. final heading quality
9. final output validation

任何阶段失败都不触碰正式输出。

### 3. 发布与回滚

- 工作目录和正式输出必须位于同一文件系统。
- 发布时先将旧正式输出重命名为临时备份，再将工作目录重命名为正式输出。
- 第二步失败时立即把临时备份恢复为正式输出。
- 发布成功后删除临时备份。
- 发布成功后工作目录因重命名成为正式输出，不再保留重复副本。

### 4. 失败恢复

- 阶段失败时打印工作目录路径和续跑命令。
- 默认下次运行复用现有工作目录。
- 解析任务继续依赖工作目录中的 `tasks.json`。
- 若需要从正式输出重新开始，使用 `--fresh-work --skip-parse`。

## 验收

- 阶段失败时正式 `output/` 的内容与运行前一致。
- 成功时正式 `output/` 只包含通过最终质量门的完整结果。
- 发布重命名失败时旧正式输出自动恢复。
- 已存在工作目录可以继续运行。
- `--fresh-work` 能重新初始化工作目录。
- 全库正式命令最终保持 `0 FAIL / 0 WARN` 和 0 output issue。

## 实施结果

状态：已实现。

- 新增 `pipeline_workspace.py`，负责工作区初始化、APFS 克隆、续跑、发布和回滚。
- `mineru-run-pipeline` 的所有子阶段均改为写入工作目录。
- 新增 `--work-dir` 与 `--fresh-work`。
- 发布前会把根级报告中的 staging 路径规范为正式输出路径。
- 新增 9 个事务工作区测试；连同章节范围测试共 13 项全部通过。
- 真实全库失败回归：故意保留 7 个 WARN 后，命令退出，正式文件 SHA-256
  与修改时间均未变化，工作目录完整保留。
- 直接复用失败工作目录重新运行后，44 本达到 `0 FAIL / 0 WARN`、0 output
  issue，随后成功发布；发布后 staging 目录不存在。
