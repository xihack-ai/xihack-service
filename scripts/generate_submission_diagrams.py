"""Generate the submission SVGs from the implemented architecture (stdlib only)."""
from pathlib import Path
from html import escape

OUT = Path(__file__).resolve().parents[1] / 'docs' / 'submission'
OUT.mkdir(parents=True, exist_ok=True)

class Diagram:
    def __init__(self, title, subtitle, height):
        self.parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="{height}" viewBox="0 0 1600 {height}">', '<defs><marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto"><path d="M0,0 L10,5 L0,10" fill="#64748b"/></marker></defs>', f'<rect width="1600" height="{height}" fill="#f5f7fc"/>', '<g font-family="Microsoft YaHei, sans-serif">']
        self.text(64, 72, title, 36, '#172554')
        self.text(64, 115, subtitle, 19, '#475569')
    def text(self, x, y, value, size=21, color='#334155'):
        self.parts.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}">{escape(value)}</text>')
    def box(self, x, y, w, title, lines, color='#6366f1', h=145):
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="16" fill="white" stroke="{color}" stroke-width="2"/>')
        self.text(x+22, y+37, title, 25, color)
        for i, line in enumerate(lines): self.text(x+22, y+73+29*i, line, 19)
    def edge(self, points):
        self.parts.append(f'<polyline points="{points}" fill="none" stroke="#64748b" stroke-width="2.5" marker-end="url(#arrow)"/>')
    def save(self, name):
        (OUT / name).write_text(''.join(self.parts) + '</g></svg>', encoding='utf-8')

d = Diagram('HireSpec Loop · 系统架构', '需求校准 → 证据评估 → 反馈提议 → 人工审批 → 局部重评；依据当前实现绘制', 1030)
d.box(64,170,400,'浏览器 · Next.js 工作台',['HR / 用人经理 / 猎头 / HRD','需求、PDF 上传、证据、审批'])
d.box(600,170,390,'Nginx · 团队演示入口',['HTTP 网站 + 演示账号认证','静态前端；/api/ 转发后端'])
d.box(1120,170,410,'FastAPI · 业务接口',['输入校验、任务与版本管理','后端 127.0.0.1:8010'])
d.edge('464,240 600,240'); d.edge('990,240 1120,240')
d.box(64,440,400,'AnyDoc · 本地文件解析',['PDF / DOC / DOCX / RTF → 文本','扫描件需另接 OCR，不虚构内容'])
d.box(600,440,390,'LangGraph · 校准 Agent',['澄清、证据提取、审核与提议','interrupt 等待人工确认 / 审批'])
d.box(1120,440,410,'DeepSeek 官方 API',['接入个人 DeepSeek API Key','HTTPS + JSON；模型异常报错'])
d.edge('1325,315 1325,365 265,365 265,440')
d.edge('1200,315 1200,390 795,390 795,440')
d.edge('990,510 1120,510')
d.box(600,710,930,'PostgreSQL 17 · Docker · xks',['业务：jobs / spec_versions / candidates / evaluations / feedback', 'change_proposals / candidate_labels / audit_events', '工作流：PostgresSaver checkpoint；数据库仅监听本机 5432'], '#0f766e', 185)
d.edge('795,585 795,710'); d.edge('1480,315 1560,315 1560,680 1410,680 1410,710')
d.box(64,710,400,'程序负责可信边界',['逐字引用验证 + 原文位置','确定性分档，无模型总分','变更范围决定重评范围'], '#0f766e', 185)
d.text(64,970,'密钥仅配置在后端运行环境；未接入赛事赞助商 API。当前为团队演示，完整组织权限与 HTTPS 尚待补齐。',19)
d.save('system-architecture.svg')

d = Diagram('HireSpec Loop · Agent 工作流', '明确区分待澄清、被拦截、无变更和待审批；拒绝候选人不会自动修改正式 Spec', 1590)
d.text(64,172,'A · 需求与候选人评估',26,'#4338ca'); d.text(830,172,'B · 反馈校准与审批',26,'#4338ca')
left = [(220,'需求问答 → 标准草稿',['/requirements/clarify · 经理逐题回答','clarify → generate_spec · 保留名单与类型']), (425,'人工确认标准',['spec_confirmation · 可暂停','正式标准发布；待确认项保留']), (630,'上传简历 / 发起评估',['FastAPI 解析文件，接收文本','extract_evidence · 逐条提取']), (835,'校验原文引用',['validate_evidence · 精确匹配','无效引用降级；缺失≠不满足']), (1040,'确定性分档与证据展示',['evaluate · 推荐 / 待核实','不推荐 / 否决；稳定排序'])]
for y,t,lines in left: d.box(64,y,620,t,lines,h=150)
for y in [370,575,780,985]: d.edge(f'374,{y} 374,{y+55}')
d.box(830,220,700,'经理提交反馈',['interpret_feedback → bias_risk_check','岗位相关性与敏感筛选条件审核'],h=145)
d.box(830,420,330,'BLOCKED · 拦截',['说明理由，编辑原反馈','不发布标准 / 不允许批准'],'#b45309',145)
d.box(1200,420,330,'NEEDS_CLARIFICATION',['向用人经理逐题追问','HR 可代填后重新审核'],'#b45309',145)
d.edge('1040,365 1040,420'); d.edge('1365,365 1365,420')
d.edge('1530,500 1570,500 1570,290 1530,290')
d.box(830,630,700,'SAFE → propose_change',['最小必要变更；生成 diff 与受影响条款','NO_CHANGE：结束，正式标准不变'],h=145)
d.edge('1180,365 1180,610 1180,630')
d.box(830,835,700,'human_approval · 人工审批',['Accept：重新校验后发布；Edit：只保存草稿','Reject：结束，正式标准不变；过期提议拒绝'],h=145)
d.edge('1180,775 1180,835')
d.box(830,1040,700,'发布 Spec v2 · selective_reeval',['语义改变 / 新增：重评受影响条款','仅调整排序类别：复用证据；删除：移除排序影响'],h=145)
d.edge('1180,980 1180,1040')
d.box(830,1245,700,'完成新版评估 → 整体切换排名',['保留历史版本、审批记录和审计轨迹','任务失败保留上一版完整结果，不混用版本'],'#0f766e',145)
d.edge('1180,1185 1180,1245')
d.edge('684,1115 755,1115 755,200 1180,200 1180,220')
d.box(64,1245,620,'评估对象与可信边界',['候选人 × 条款：结论 + 原文证据 + 疑点','原文存在不等于推理正确，仍需人工抽查','模型失败作为任务错误，不用模拟结果替代'],'#0f766e',145)
d.text(64,1490,'反馈问答留在同一会话；经理回答后重新审核。候选人上传和问答入口由 FastAPI 调用相应工作流路径。',19)
d.text(64,1530,'此图为实现概览；图中的人工决策和接口入口不全部对应独立 LangGraph 节点。',19)
d.save('agent-workflow.svg')
