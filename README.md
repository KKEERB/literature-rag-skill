# 文献 RAG 工作流 Skill

一个面向太空物理的 Codex skill，帮助研究者用自己的本地 PDF 建立独立知识库：原文两轮处理、带来源的检索、按需精选精读与人工条目审阅。用户可以分别选择两轮的模型与思考深度，并随时为新批次修改设置。首版不包含 Zotero 接入或全文翻译管线。

当前内置 `sxi_cusp`、`fte_knowledge` 和 `magnetosphere_ionosphere` 三种研究 profile；精选精读采用太空物理训练模板。其他学科的完整精读及工作区自定义 profile 尚未支持或验收。

> **首版试用。** 核心流程已在 macOS 上用虚构材料检查；Windows 不在本版支持范围内，模型底层身份没有可信执行回执。软件不能把工作单、模拟审阅或文件存在当作实际的论文阅读与科学接受。

## 安装

将本仓库链接交给自己的 Codex，并发送：

> 请从 `https://github.com/KKEERB/literature-rag-skill` 的 `skills/literature-rag-workflow` 安装 skill。安装后请引导我在自己的电脑配置文献工作区，以及第一轮和第二轮模型与思考深度。

Codex 可使用其 skill 安装器，仓库内 skill 的确切位置是 `skills/literature-rag-workflow`。安装器仅复制这个目录，其中包含运行所需资源。安装后从下一轮对话使用；若同名目标已存在，不覆盖，应先检查和处理冲突。

## 你可以怎样使用

1. **首次配置：**选择独立工作区与存储位置；检查 Python、SQLite FTS5、PDF 工具、所需预检器和字体。分别选择分析和复核的模型与思考深度，系统能力不可用时报告原因。
2. **快速入库：**指定你有权使用的 PDF。第一轮分析原文，第二轮以独立任务复核原文；只有满足来源与质量条件的条目进入检索。
3. **检索：**按问题查询本地索引，并回到来源、页码和原文上下文检查；缺证据时不把摘要当作确定事实。
4. **精选精读：**选择需要深入阅读的论文，生成或复用 Reader Note 和研究卡。你逐条接受或不接受；异议和适用范围会保留。
5. **更改模型：**对 Codex 说“查看文献处理模型设置”或“修改文献处理模型设置”。更改仅作用于新批次；已经启动的批次继续使用其冻结配置。

人工认可属于当前条目的判断，不代表整篇文章的科学机制已经证实。机器条目仍应回查原文；自动复核不是人工精读。

## 环境与资料边界

首版面向 macOS，核心流程已使用虚构材料检查；Windows 不在本版支持范围内，尚未进行真机验收。工具包括 Python、可用 SQLite FTS5、Poppler 命令、Python PDF 处理包及固定版本 PDF 完整性预检器；部分 PDF 还需要 `qpdf`。

工作区、数据库、缓存和审阅记录属于你自己，仓库不包含现成的真实文献库或论文。预检器缺失、版本不符、模型设置不可用或数据身份不清时工作流停止并报告。数据库保存在本机不等于 Codex 的模型处理一定离线；请依照所使用服务和账户设置决定哪些论文可以处理。

首版使用本地 PDF，不包含 Zotero 发现、选文、同步或写回。精读工作单存在不代表完整精读已经完成。

## 许可

项目原创代码按 MIT 许可发布。随附的 `academic-research-skills` PDF 预检器及其伴随代码属于第三方材料，版权归 Cheng-I Wu，适用 **CC BY-NC 4.0**；许可原文与 NOTICE 位于 `skills/literature-rag-workflow/runtime/third_party/academic-research-skills/`。因此整个包不能统一称为 MIT，随附的预检器不得用于商业目的。Python 软件包、字体、模板及其他素材分别适用各自许可；不得悄悄移除必要的完整性检查。

随附的预检器、伴随代码、测试及许可文件来自 [Imbad0202/academic-research-skills](https://github.com/Imbad0202/academic-research-skills)，固定来源为提交 [`7f878f7ad2eb8c1be3d2fffebeee74362fa79d02`](https://github.com/Imbad0202/academic-research-skills/tree/7f878f7ad2eb8c1be3d2fffebeee74362fa79d02)。这五个随附文件与该提交的对应文件逐字节一致；项目为调用预检器编写了独立的适配代码。

安装包内的 [依赖与许可说明](skills/literature-rag-workflow/runtime/third_party/DEPENDENCIES.md) 列出主要 Python 依赖和外部命令的来源及许可；其各自的许可条件仍然适用。
