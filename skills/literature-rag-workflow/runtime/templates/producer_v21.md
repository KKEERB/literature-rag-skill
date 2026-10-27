# Pipeline v2.2 单篇 Producer Contract

- 一次只处理一个来源 PDF，不得把其他论文的内容、claim、方法或图表带入当前模型。
- 只写本次精读任务指定的工作区阶段目录，不得覆盖已发布产物。
- 完整读取 normal/layout/selected 分页文本与原 PDF；低质量页只能使用 manifest 中有 provenance 的 OCR。
- 仅当 normal/layout 因双栏重排无法保存连续原句时，可增加显式 `raw` route；必须记录 `pdftotext -raw` flags、独立文件路径与 normalized checksum，禁止把 raw 内容冒充 normal/layout。
- 生成 4–8 条论文特定中文 claim、至少 2 条具体方法、3–6 张实际视觉复核图、10–12 个论文特定自检题。
- 填写 `reader_note.template_version=space_physics_research_training_v1`：一句话摘要、
  3–5 个导航问题、1–3 项核心工作、背景缺口、四层物理机制、第一性原理物理审查、
  类型化方法审查、贡献审查、批判性审查、课题迁移和 10 分钟组会摘要必须完整。
- `reader_note.core_work`、第一性原理 claim 分级、贡献与课题迁移必须引用当前模型中
  已存在的 claim/method/project-relation ID；不得用 reader-facing 综合新增无锚点事实。
- reconnection、FTE、cusp、MHD、hybrid、PIC、theory 默认启用第一性原理审查。
  纯统计型观测可标 `optional` 或 `not_applicable`，但必须记录理由；该模块不得冒充新证据。
- 每幅 Figure/Table 必须说明它回答的科学问题、读图顺序、直接可见结果、物理意义、
  支持的 claim、替代解释和易误读点；不得机械翻译 caption。
- 每个 `anchored` 对象必须记录精确 PDF 页、实际 section/figure/table、8–40 词可回查 quote、`extraction_route` 和 `page_checksum`。
- quote 不仅要能在页面中匹配，还必须完整覆盖该对象的全部事实与机制；一个 quote 只承载一个原子 claim/method/limitation/answer。若陈述包含两个独立结果、条件或步骤，必须拆分对象或缩窄陈述，不能用局部 quote 锚定复合结论。
- P0 或 `substantive: true` 争议才联网；只外发题名、DOI 和 claim 查询，不上传 PDF；使用 2–5 个已核验来源。
- 若 project profile 定义 `grading_policy`，必须在完整阅读后填写五维 0–2 分 `grading`；P0/P1/P2 只作 triage 输入。producer 阶段保持 `reviewer_confirmed: false`、`reviewer_task_id: ""`。
- `generation.producer_task_id` 必须是本 producer 的 canonical task name；`reviewer_task_id` 留空。
- `generation.prompt_version` 使用当前 quality profile 的值；旧 prompt 仅为历史兼容，
  不得用于新 producer 规避新版 Reader Note 契约。
- 结束前运行 producer-phase semantic validator，只有 `status=pass` 才能交给 reviewer。
