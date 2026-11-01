// 模拟数据（无后端）。真实项目由 NestJS 提供，前端经 api 层 + OpenAPI 类型调用。

export const kpis = [
  { label: '待办线索', value: '38' },
  { label: '跟进中商机', value: '21' },
  { label: '在途金额', value: '¥ 486K' },
  { label: '赢单率', value: '31.4%', delta: '+2.1%', up: true },
  { label: '平均跟进周期', value: '5.8 天', delta: '-0.4 天', up: false }
]

export const stages = ['意向确认', '方案报价', '谈判', '赢单']

export const opportunities = [
  { id: 'OPP-2406-1183', name: '远途供应链 · ERP 集成采购', customerId: 8841, customer: '远途供应链(上海)', level: 'A', stage: '谈判', amount: 128000, src: '企微·广告留资', wait: '逾期 1.2 天', priority: 'high', owner: '张文' },
  { id: 'OPP-2406-1190', name: '宏图教育科技 · 年度续约扩容', customerId: 9001, customer: '宏图教育', level: 'A', stage: '方案报价', amount: 96000, src: '官网表单', wait: '等待 4 小时', priority: 'high', owner: '张文' },
  { id: 'OPP-2406-1201', name: '青柚生鲜 · 多门店版试用转化', customerId: 9002, customer: '青柚生鲜', level: 'B', stage: '意向确认', amount: 54000, src: '抖音·Automan', wait: '等待 1 天', priority: 'mid', owner: '张文' },
  { id: 'OPP-2406-1205', name: '瀚海物流 · 单仓试点', customerId: 9003, customer: '瀚海物流', level: 'B', stage: '方案报价', amount: 42000, src: '微信', wait: '等待 1 天', priority: 'mid', owner: '张文' },
  { id: 'OPP-2406-1210', name: '川流文旅 · 复购意向', customerId: 9004, customer: '川流文旅', level: 'B', stage: '赢单', amount: 36000, src: 'CSM 回流', wait: '等待 3 天', priority: 'low', owner: '张文' }
]

export const followUps = [
  { id: 1, by: 'Hermes 销售 Agent', ai: true, channel: '企微', time: '06-18 09:12', text: '自动跟进：发送旗舰版集成方案与 ROI 测算，客户已读并询问实施周期。', trace: 'traceId 7f3a·c19e | SOP 节点 方案跟进-D3 | 回写自 Hermes' },
  { id: 2, by: '张文', ai: false, channel: '电话', time: '06-18 15:40', text: '电话沟通 22 分钟：确认预算 12–13 万，决策人为采购总监，要求 8.5 折并含一年免费实施。', trace: '' },
  { id: 3, by: '张文', ai: false, channel: '阶段推进', time: '06-18 15:52', text: '阶段由「方案报价」推进至「谈判」；预计成交日更新为 07-05。', trace: '' },
  { id: 4, by: 'Hermes 销售 Agent', ai: true, channel: '企微', time: '06-19 10:03', text: '按 SOP 发送谈判期素材；检测到「价格敏感」标签，未自动承诺折扣，转人工。', trace: 'traceId 91b2·44d0 | 权限校验：折扣超限→挂起 | 回写自 Hermes' }
]

export const customerPanel = {
  level: 'A 级 · 战略客户',
  deals: '¥ 612,000 · 4 笔',
  tags: ['制造业', '华东', '决策周期长'],
  risk: ['价格敏感'],
  csm: '近 30 天 1 个工单（已解决），满意度 4.6/5，无重大投诉。'
}

export const leads = [
  { id: 'L-001', name: '未命名', contact: '138****6627', src: '抖音 · Automan', campaign: '夏季获客 #A-411', customer: '新客（待识别）', status: '新建', statusType: '', owner: '— 待分配', dedup: '通过', dedupType: 'success', created: '5 分钟前', priority: 'high' },
  { id: 'L-002', name: '李工', contact: '苏州智造装备', src: '官网表单', campaign: '官网·方案下载', customer: 'customerId 9023', status: '跟进中', statusType: 'primary', owner: '张文', dedup: '通过', dedupType: 'success', created: '2 小时前', priority: 'mid' },
  { id: 'L-003', name: '未命名', contact: '微信留资 wx_8842', src: '微信', campaign: '朋友圈广告', customer: '新客（待识别）', status: '待分配', statusType: 'warning', owner: '— 待分配', dedup: '通过', dedupType: 'success', created: '6 小时前', priority: 'low' },
  { id: 'L-004', name: '王敏', contact: '131****2098', src: '企微 · 广告留资', campaign: '计划 #A-307', customer: 'customerId 8841', status: '已转化', statusType: 'success', owner: '张文', dedup: '命中·关联 L-7790', dedupType: 'warning', created: '1 天前', priority: 'low' }
]

export const playbooks = [
  { id: 'PB-1', title: '旗舰版·供应链集成卖点', desc: 'ROI 测算 + 实施周期话术', scope: ['A 级', '企微'], version: 'v3', period: '长期', rag: '已索引', ragType: 'success', status: '生效', statusType: 'success' },
  { id: 'PB-2', title: '年中大促·折扣政策', desc: '满 8 万享 9 折，上限 8.5 折需审批', scope: ['全等级', '全渠道'], version: 'v2', period: '06-01 → 06-30', rag: '已索引', ragType: 'success', status: '活动期', statusType: 'warning' },
  { id: 'PB-3', title: '竞品对比·对 X 厂商', desc: '数据同步 / 私有化部署优势点', scope: ['A/B 级'], version: 'v5', period: '长期', rag: '已索引', ragType: 'success', status: '生效', statusType: 'success' },
  { id: 'PB-4', title: '多门店版·试用转化话术', desc: '', scope: ['B 级', '抖音'], version: 'v1', period: '草稿', rag: '未索引', ragType: 'info', status: '草稿', statusType: 'info' }
]

export const sopScenarios = [
  { code: 'SOP-LEAD-INTAKE', name: '新线索承接', version: 'v4', active: true },
  { code: 'SOP-REVIVE', name: '沉默唤醒', version: 'v2', active: false },
  { code: 'SOP-QUOTE-APPROVAL', name: '报价审批', version: 'v3', active: false },
  { code: 'SOP-WON-HANDOFF', name: '赢单交接', version: 'v2', active: false }
]

export const sopNodes = [
  { n: 1, name: '即时响应', tag: 'Hermes 自动', tagType: 'primary', rules: ['触发：线索创建', '间隔：≤ 5 分钟', '话术：欢迎+需求确认'] },
  { n: 2, name: '需求澄清', tag: 'Hermes 自动', tagType: 'primary', rules: ['间隔：D1 / D3', '升级：2 次无响应→转人工'] },
  { n: 3, name: '报价', tag: '权限边界', tagType: 'warning', rules: ['报价上限：标准 9 折', '＜9 折：必人工节点'] },
  { n: 4, name: '转商机', tag: '里程碑', tagType: 'success', rules: ['条件：明确预算+决策人', '动作：创建商机·进入阶段流'] }
]

export const funnel = [
  { label: '线索', value: 1284, pct: 100 },
  { label: '有效跟进', value: 1078, pct: 84 },
  { label: '商机', value: 550, pct: 51 },
  { label: '报价', value: 389, pct: 38 },
  { label: '赢单', value: 173, pct: 24 }
]

export const perf = [
  { name: '张文', amount: 486, pct: 92 },
  { name: '李航', amount: 372, pct: 71 },
  { name: '周琳', amount: 305, pct: 58 },
  { name: '陈默', amount: 231, pct: 44 }
]

export const roles = ['销售坐席 R1', '销售主管 R2', '运营管理员 R3', '集成账号 R4(Hermes)']
