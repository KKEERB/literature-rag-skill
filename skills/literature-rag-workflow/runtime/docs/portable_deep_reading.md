# 精选精读任务：不含全文翻译

面向用户明确选定的论文，独立回读完整原 PDF 及原文页面；快速入库摘要是索引线索，不是已完成的全文精读。读取 `paper_manifest.json`、`project_profile.json`、`quality_profile.json`、`extraction/extraction_manifest.json` 与 schema。`model.scaffold.json` 是待填写的结构示意，不是结果或来源证据。

生产者根据原文填写 schema v2 `02_machine_evidence_pack.json`：保留研究问题、方法、结果、作者解释、限制、图表及精确页段的出处，逐层区分原文事实与模型推断。填写 Reader Note 科研训练内容和研究卡；不适用的模块说明原因，不能用不存在的图表、数据或物理机制凑数量。图像须实际查看后才能标记视觉审阅；文字提取或图注不能代替视觉阅读。

独立复核者先重新阅读原 PDF，再检查生产者模型的完整论点、条件、数值单位、归属、认识论层与定位；保留遗漏和冲突。写出 `review/review_report.json`，至少包含独立真实 task ID、exact model file SHA-256、`model_content_sha256`、原 PDF 路径与 SHA-256、六项实质检查及明确 decision。不得因为测试需要而虚构通过；producer/reviewer 均为机器，状态保持 `draft / NOT HUMAN CHECKED`。

身份以 `paper_manifest.json` 和原 PDF SHA-256 为准；模型与复核报告应绑定同一来源，不得制造来源身份。

仅在实际模型和独立复核满足质量门后，以现有 v2 确定性渲染器生成 Reader Note 和调研卡。明确区分模型复核与用户逐条接受；无人工实际回复时不记录人为通过。此流程不要求生成全文翻译或译文 PDF。

在完成生产和独立复核后，使用以下入口检查来源、模型与复核绑定并生成两条笔记：

```text
python runtime/scripts/portable_deep_read.py finalize \
  --stage <工作区中的精读阶段目录> --workspace <工作区>
```

质量门未通过或已有产物发生冲突时停止；完成渲染不表示人工接受。
