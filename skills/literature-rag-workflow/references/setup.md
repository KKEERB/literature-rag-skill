# 首次配置和模型选择

首版面向 macOS；Windows 不在本版支持范围内，尚未进行真机验收。当前系统的实际可用性以本次诊断和运行验证为准。确定用户独立的工作区、数据库、批次和存储根，不复用其他人的数据库、审核记录、自动任务或机器路径。不要覆盖现有配置或安装同名 skill。

1. 使用当前 Python 执行 `runtime/scripts/onboarding_doctor.py`，检查 Python 依赖（包括用于页面渲染的 `pypdfium2`）、SQLite FTS5、`pdfinfo`、`pdftotext`、`pdftoppm`；部分 PDF 的完整性预检还需要 `qpdf`。预检器位于 `runtime/third_party/academic-research-skills/scripts/`，适用其附带的 CC BY-NC 4.0 许可；其他依赖的来源和许可见 [依赖说明](../runtime/third_party/DEPENDENCIES.md)。缺少固定 SHA 的预检器或它的伴随依赖时保留 `UNAVAILABLE`，不得绕过预检。
2. 在用户选定的独立目录中运行 `runtime/scripts/portable_config.py --workspace <工作区>`。它创建隔离配置，不继承其他人的来源或人审记录。对每个运行流水线的进程将 `LITERATURE_RAG_HOME` 指向这个工作区；仅在进程环境中设置，不改系统全局配置。路径、编码及文件权限必须通过当前平台真实验证。
   - macOS shell 当前进程：`export LITERATURE_RAG_HOME='<工作区绝对路径>'`。
   - 首次配置只创建配置文件，并不创建知识数据库。仅当这是**新建空工作区**且确认 `<工作区>/knowledge.sqlite3` 不存在时，运行 `python runtime/scripts/knowledge_system.py --config <工作区>/config/knowledge_system.json --db <工作区>/knowledge.sqlite3 sync --json`，检查返回的完整性与空库状态。`sync` 会重建索引；已有数据库或来源文件时不得将它当作无条件初始化，先检查现有库并遵循用户授权。
3. 询问第一轮分析、第二轮复核各自的模型 ID 和 reasoning effort；可用组合取自宿主当前能力，无法获知时让用户提供，不硬编码模型价格、额度或可用性。两个请求分别调度并记录，缺少独立调度时不能冒充两轮完成。
4. 使用 `runtime/scripts/model_settings.py` 的 `set` 保存到工作区 `config/model_settings.json`，使用 `show` 查看。首次 `set` 要同时提供 `--analysis-model`、`--analysis-effort`、`--review-model`、`--review-effort`；之后可以只传需要修改的字段。每次提供 `--path` 指向用户工作区内的设置文件。设置改变后新批次采用新设置；现有批次绑定原快照。
5. 用用户有权使用的少量 PDF 和独立数据库开展真实试验，确认原文追溯、中文和含空格路径、审核与异议。模拟检查不等同论文科学审核。

目前内置 profile 仅覆盖太空物理：`sxi_cusp`、`fte_knowledge` 和 `magnetosphere_ionosphere`。精选精读使用太空物理研究训练模板；尚无工作区自定义 profile 的支持和其他学科的完整验收。明确选择匹配研究主题的现有 profile，不自动默认 SXI。

本地持久化与模型处理是两件事。处理非公开 PDF 前，用户需按自己使用的 Codex 服务、账户和权限确认适合的材料范围。不要替用户添加凭据或创建后台任务。
