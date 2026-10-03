# PDF 上传与队友补丁整合

合并基于后端 `fc66f0a` 与前端 `d1d006b`，按功能合并旧补丁，没有用旧文件覆盖新上传代码。

## 页面接口

活动工作台使用 `/jobs` 与 `/calibrations`；保留另一套 `/api/hiring` 契约，未混用其 JSON 模型。

- `evaluations[].judgments`：普通条款 SATISFIED / NOT_SATISFIED / NO_EVIDENCE；VETO HIT / CLEAR / UNKNOWN。UNKNOWN 进入待核实，CLEAR 要有反证，冲突证据进入待核实。`scores` 只用于兼容与确定性排序，不是总分。
- 反馈支持 add / remove / update。update 保留 ID。同版本已拒绝的相同结构变更不重复提出，不宣称语义去重。
- `/proposal/edit` 保存完整编辑草稿，当前标准与排名不变。之后 `/approval` accept 才发布；旧 action=edit 的直接批准接口保留兼容。
- 接受与编辑都复检结构、重复标准、敏感条件与历史 ID。客户端发送 base_spec_version，过期返回 409。
- 定义变化和新增只重评受影响条款；类型、权重和删除仅重新聚合。所有已评估候选人重排，待评估文件保持待评估。
- `proposal_impact` / `recomputation` 是逻辑评估单元计数，不代表模型请求数、缓存命中统计或耗时。

## PDF 保留行为

文件本地解析，支持 PDF/DOC/DOCX/RTF，每批最多50份、单份10MB、整批50MB。解析失败逐文件显示；扫描版需要 OCR，当前不自动 OCR。重复解析文本在同职位内去重。

上传不调用模型、不确认标准、不触发评估。确认标准后，点击“开始评估”显式评估未处理文件。模型失败保留已完成结果并报任务错误，可重试。

引用必须在**同一份不可变解析文本**中逐字存在；不在校验时删除空白或转换连字符。一对候选人×标准中任何引用失效，整对降级，不能保留部分引用拼出满足结论。生成标准继续验证 source_quote，不能将“高级”变为自行增加的固定年限。

## 模型、审核与缓存

- 未配置密钥或模型错误：明确报错，无生产 mock 或关键词匹配兜底。
- `HIRELOOP_MODEL_MODE=LIVE`：真实调用，不读写录制缓存。
- `RECORD`：真实调用并原子保存缓存；保存失败报错。
- `REPLAY`：严格匹配录制记录，不联网，缺记录报错。初次上传解析仍用真实文件解析器。
- 未显式设置模式时，保留现有有密钥的自动缓存行为；REPLAY_ONLY=1 阻止真实调用，不再生成模拟标准。
- 保留 LLM_CACHE_FILE、官方 DeepSeek thinking 配置和版本隔离的缓存键。录制缓存包含简历与模型输出，必须忽略 Git。
- `HIRELOOP_CONCURRENCY=4`（1～8）：候选人并发，单条评估不接收排序类别。单进程缓存写入加锁；多进程共享缓存仍需独立实现文件锁或数据库缓存。
- 安全检查使用显式条件规则和上下文模型复检，产品的“女性用户”“婚育服务”不因单字命中而直接拦截；模型审核失败不能放行。
- `HIRELOOP_SEMANTIC_REVIEW=1` 可开启额外独立语义审核，默认0以保留原调用成本。它逐候选人×条款检查新证据，复用的旧证据不重复审核，审核失败报错。模型审核仍不是人工真值，也不能修复全部漏检。

## 独立标签评测

`python scripts/evaluate_judgments.py results.jsonl --output report.json`。

每行含 candidate_id、criterion_id、definition_version、kind（criterion/veto）、expected（独立人工标签）、predicted、resume_text、quotes。失败记录使用 predicted=null 与 error；不能把超时标成 NO_EVIDENCE。

分别报告完成调用的 Accuracy/Macro-F1/各类别P/R/F1与混淆矩阵，所有引用类别逐字验证，并报告必需证据缺失、虚构引用、任务失败率及把失败计入分母的 end_to_end_accuracy。没有人工标签不填真实准确率或 Precision@5。数据划分可参数化；正式计划仍建议冻结10份校准+30份测试，测试标签不能用于校准。

## 验证边界

### 待核实问题的逐题反馈（新增）

确认初始 Spec 后，`POST /calibrations/{job_id}/clarification-feedback` 接收 `question_index`（从 0 开始）、`category`（`MUST_HAVE` / `NICE_TO_HAVE` / `NOT_REQUIRED`）、`answer`、`base_spec_version` 和可选 `created_by`。问题序号必须来自当前职位的 `clarification_questions`；必须项和满意项需要具体答案，过期版本或待审批提案返回冲突。`GET /calibrations/{job_id}` 中的 `clarification_responses` 供前端显示逐题状态。

必须项/满意项沿用原有反馈解释、偏见检查、变更提案和人工审批图；选择类别会约束提案条款类型，批准前 Spec 与排名不变。非必要仅记入 checkpoint 和审计，不调用模型、不新增标准。若已有标准需要删除，仍须单独提出删除提案并审批。此处“待核实问题”指职位需求的歧义，不等同于候选人缺少证据的“待核实”档位。

本轮单元/API/LangGraph 测试使用显式测试替身；生产没有恢复模拟数据。真实模型与 PDF 的本机检查另列测试记录，不能混同规则测试与真实模型质量。

未自动更新服务器。PostgreSQL 重启恢复、多进程并发锁、可恢复后台任务和完整新版排名原子发布仍须单独集成验收。HTTP 当前同步执行；旧评估可能在重评任务进行时留在 checkpoint，不能把进行中快照当成完整新版结果。部署应使用单 worker，失败先恢复当前任务，不应在结果不齐时对外宣称新版排名完成。
