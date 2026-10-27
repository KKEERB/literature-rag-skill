# Pipeline v2.2 独立 Reviewer Contract

- 只复核指定的一篇 producer model；不得修改 `02_machine_evidence_pack.json` 或替 producer 重写内容。
- 重新读取 PDF 分页文本、关键图图像和 producer semantic report；抽查全部 claim/method anchor、全部视觉图和至少 6 个问题。
- 若使用 `raw` route，复跑 `pdftotext -raw` 并核对 flags、文件与 normalized checksum；不得接受以 normal/layout 名义保存的 raw 变体。
- 检查研究问题—方法—证据—结论是否一致，作者解释/直接结果/项目迁移是否分层，替代解释和不能推出的结论是否充分。
- 检查新版 Reader Note 的 14 模块是否由同一个 canonical model 渲染：导航不复述摘要，
  核心工作不凑数，物理机制有因果链，方法审查随论文类型切换，图表解释回答科学问题。
- 对第一性原理模块单独检查：基本规律不是空泛术语；假设和 claim 分级引用有效 ID；
  `strong_support / reasonable_inference / model_dependent / not_demonstrated` 没有越过原证据；
  纯统计论文若关闭模块，理由必须成立。
- 检查贡献/发表价值是相对既有工作的证据化判断而非真实审稿结论，课题迁移仍为
  `project_application`，10 分钟组会摘要没有删除关键限制。
- 对 P0/实质争议，核查 2–5 个外部来源的角色、链接和证据层级；不得把外部结果混成论文原文。
- 对启用 grading 的项目，逐项复核五维分数、总分、P0/P1/P2 阈值、核心支持类型和例外理由；`checks` 中必须有一个 `id` 含 `grading` 的明确检查。
- 仅写 `review/review_report.json`。`decision` 只能是 `pass`、`revise` 或 `blocked`；最多两轮修订。
- `pass` 必须满足：无 `blocking_findings`；记录 producer/reviewer task IDs、`revision_round`、`model_content_sha256`、逐维 checks 和具体抽查锚点。
- reviewer 与 producer 必须是不同 task；reviewer 不渲染，也不发布或晋升内容。
