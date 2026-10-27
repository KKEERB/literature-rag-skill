# 文献处理操作

调用脚本前读取它们的 `--help` 并使用用户工作区的绝对路径，保留源 PDF 身份和指定数据库、批次及存储根。候选配置和脚本属于可移植副本，不使用构建者机器的默认路径。

使用用户提供或授权的本地 PDF 完成入库、来源检索、精选精读和人工审阅；首版不包含 Zotero 接入。

## 两轮快速入库

`runtime/scripts/literature_learning.py intake` 准备；详细批次入口为 `runtime/scripts/fast_pdf_intake.py prepare/status/publish`。向 intake 或 prepare 用 `--model-settings` 指定用户独立的模型设置文件，同时显式指定数据库和存储根。prepare 返回工作单后，实际启动两次独立 Codex 模型任务：第一轮处理来源，第二轮重新对照来源并检查条件、单位、归属和边界。若运行环境不能在独立任务中按选择传递模型与思考深度，标记未完成；不能只写 JSON 冒充处理。

直接输入 PDF 时必须显式指定已安装的研究 profile，例如：

```text
python runtime/scripts/literature_learning.py intake --pdf <原PDF> \
  --project-profile magnetosphere_ionosphere --output <工作区>/batches/example \
  --database <工作区>/knowledge.sqlite3 --storage-root <工作区>/fast_pdf \
  --model-settings <工作区>/config/model_settings.json
```

其中 `magnetosphere_ionosphere` 是适用于磁层—电离层研究的示例；应根据论文选择 `sxi_cusp`、`fte_knowledge` 或 `magnetosphere_ionosphere`。其他学科尚无经完整验收的 profile，不能虚构名称绕过检查。manifest 输入须为每篇提供可用的 `project_profile`；不存在或不安全的 profile 会在创建新批次和抽取 PDF 前被拒绝。

对新批次，按 `fast_pdf_intake.py record-dispatch --help` 为两轮分别记录宿主返回的 `task_id`、调用方使用的 `host_tool`，以及宿主实际提供的模型或深度。缺少两轮任务记录时不能完成验证或发布；不要由模型自报或虚构宿主回执。记录只是调用方提供的信息，不能证明底层服务模型身份。宿主不能按指定设置调度或观察到的设置与请求冲突时停止，不静默替换。

`publish` 未加 `--confirmed` 不提交数据库，但可能写候选派生文件；预演应使用用户工作区或隔离临时存储。正式发布前根据当前用户请求和权限确认其目标范围；条目必须通过完整性和独立复核。不可用、存疑或被隔离项目不得当作已通过。

## 查询、精读和人工审阅

使用 `runtime/scripts/knowledge_system.py` 的 `status/search/get`，显式指定该工作区的配置和数据库。记录实际来源、页码及条件；检索命中不等于来源已获人类接受。

用户选定精读时使用 `literature_learning.py select` 和 `reading_sessions.py`，再运行 `portable_deep_read.py prepare --session <会话目录> --workspace <工作区>` 准备独立的精读任务。核对原 PDF 身份并实际完成独立生产与复核后，运行 `portable_deep_read.py finalize --stage <精读阶段目录> --workspace <工作区>`；该命令重新执行质量门，通过后才生成 Reader Note 和调研卡。本流程不生成全文译文。`review_checklists.py` 支持逐条展示和记录接受／不接受；保留未回答、异议及版本绑定。不得把模型生成的审阅当作人的选择。

联网检索、发送或发布信息，各自依照用户明确授权范围和实际可用工具执行。提示用户数据处理可能涉及的远程服务，不假定全部计算离线。
