# 运行器日志、配置来源与执行前检查

运行日志保存在同一 run 中，由 `large-task-tracking` 汇总阶段和用例进度。日志说明实际发生的动作和可观察结果，不要求模型输出内部推理。

## 每次控制器调用的证据

`agent-calls/NNN-role/` 中保存请求、实际传输文本以及以下执行文件：

- `execution.json`：调用开始前写入 `starting/running`、调用序号、角色、声明的模型和调用超时；结果返回后记录响应接收、校验成功或错误。调用开始时同步 `state.json` 中的已用次数和活动调用。
- `process.execution.json`：本地进程命令、PID、工作目录、起止时间、退出码、字节数和最后输出时间，受 `schemas/v1/process-execution.schema.json` 约束。命令中的常见凭据参数被遮盖，stdin 和环境变量值不复制到此记录。
- `process.stdout.log`、`process.stderr.log`：接收后立即追加，包括尚未换行的 UTF-8 输出。与进程退出无关，硬中断前已写入的数据仍可读取。
- `process.log`：运行时的合并视图；正常结束或有界超时后保留兼容的 stdout/STDERR 排版。
- `response.txt`、`response.json`：后端返回和通过输出 Schema 校验的结果；没有文件时不能声称模型已经产出可用结果。

stdout 的 CLI 事件和 stderr 的诊断都会实时转发为 `agent_stream` 事件。`still_running` 是运行器等待心跳，不能当作模型已经取得业务进展。日志有输出也不会自动延长用户批准的总预算或角色超时。

硬杀运行器后，最后记录可能仍是 `running`。这是最后观测状态，不是进程存活证明，也不是终态证明。恢复前必须由宿主核对 PID/进程终态和已用预算；运行器会拒绝从尚未核算的旧活动调用直接启动新调用。不得把文件时间、心跳或单次观察超时当作终止证据。
如果有界清理后仍无法确认子进程退出，记录 `termination_unconfirmed`，保留空的结束时间，并阻止自动恢复。

## 逐案进度与最终验收

执行器的 `case_progress` 事件标明已冻结的 `case_id`，并在可确定时附带 `suite`（baseline、regression、acceptance）、修复轮次和 executor。`started` 表示开始；`provisional_pass`、`provisional_fail`、`provisional_error` 是从上游流式日志识别的暂定状态，不是最终判定。完成后以结果文件对账，发送带证据引用的 `final_pass`、`final_fail` 或 `final_error`。运行中断且无法对账时，已开始但尚未终结的用例会发出没有结果证据引用的 `final_error`；未知或不符合冻结顺序的日志不能构成用例结果。

逐案 `final_pass` 是该次评测结果，仍须通过完整回归、独立验收和最终 `verify`。只有 `verification.json` 的 `status=verified` 才能报告整个候选完成；未运行、证据缺失或阶段失败不得显示为通过。`large-task-tracking` 负责跨轮和用户可见的进度，运行器提供阶段、逐案事件及证据。

## 单次调用继承总 deadline

创建、优化和独立比较工作流的 `max_seconds` 是整轮总 wall-clock deadline。Agent 调用、模型工具里的 shell、skill-up 执行器、本地 judge、静态 suite validator、Docker 目标探针和 managed command adapter 都使用启动该动作时的剩余总时间；不再额外套用 300/240/60/30/20 秒的固定单次上限。未指定总 deadline 且未配置显式 per-call timeout 时，`timeout_seconds` 为 `null`，不由运行器擅自截断。

只有用户显式传入 `--timeout`，或 backend profile 明确声明单次 timeout 时，才添加单次 fallback；同时存在总 deadline 时取两者较小值。运行日志记录实际传入的 timeout；空值表示没有本地单次上限，整轮总预算仍负责停止任务和防止无限运行。进程清理、环境健康检查和恢复时的进程查询保留各自的短操作保护，不得把它们误报成目标 Skill 测试的执行上限。

工具轨迹不完整时，不能根据“未观察到调用”推断没有外部调用或副作用。目前执行器不能独立证明完整的工具缺失覆盖，包含 `tool_command_absent` 或 `tool_call_absent` 的用例会阻断，而不是自动通过。

## 配置来源

安装版 launcher 通过 `configuration_origin` 记录安装清单路径、清单中的 `configured_defaults`、本次真正注入的 `applied_defaults` 和命令行显式参数名。`doctor` 展示安装默认值时不把它们误报为本次已应用的执行参数。

角色的 model、reasoning_effort、timeout 等声明值会进入预检。缺少显式设置时，报告 `unresolved_inherited_fields`；程序版本、登录状态和模型目录中的名称均不构成真实推理成功。`doctor.ready` 仍只表示本地依赖检查通过，必须同时展示 `not_live_tested` 和未解析的生效配置。预检不修改安装默认值、不选择替代模型、不自动增限。

## 预算与覆盖检查

调用模型前生成 `runtime-preflight-*.json`，受 `schemas/v1/runtime-preflight.schema.json` 约束。基础最短路径的预检估算为 create 计划阶段 7 次控制器调用、refine 计划阶段 4 次；实际还可能需要标准复核、修订或其他调用。v7 遇到响应格式适用性未决时，分类和独立复核最多各调用一次，并先以同一 Agent 实例内的原子预留核对未缓存调用预算；缓存已验证的调用不重复消耗。只有通过哈希、Schema 和原有检查点门验证的步骤才能扣减；文件存在本身不算复用证据。

分析完成后，在生成草稿前重新核对适用场景数量与两套用例上限。冻结计划后，使用实际的开发/验收用例数和 executor 数检查最低评测成本。预检不缩减场景，不改变断言或验收门。修复回归会额外耗用预算，配置的最大修复轮数不保证一定能跑满。

预检报告的 `passed` 只表示已知最低工作量能装进声明的预算；未知场景、真实推理可用性、实际配置继承值和时延仍须分别验证。时间检查只拒绝已耗尽的预算，不凭主观估计宣称剩余秒数足够。失败报告先保存，再在新模型调用或目标评测前停止。

每批评测开始前先写 `evaluation-cost.json` 预留全部 case×executor 成本，正常返回或受控错误后再记录实际消耗。未决的 `running` 账本保留全额预留并阻止恢复。恢复时合并 state、历史缓存和账本，明确去重；失败消耗不能因缺少成功缓存而清零，目标 Skill 内部的同名文件不能影响控制器账本。

成本抵扣与已用计数是两件事：只有输入 identity、结果哈希、完整且唯一的 case×executor 结果均匹配，且没有执行 ERROR 的缓存，才能免除重复评测。已完成的验收还须对应验证过的当前候选，不能拿另一个历史候选的结果抵扣。业务 FAIL 可以作为已经观察到的结果复用，但仍须按原修复/验收门处理，不能算作 PASS。

恢复时以新序号追加预检报告，不能覆盖旧报告或重绑已完成阶段的产物哈希。运行级预检历史在最终 verification 清单中统一绑定；已完成阶段再次调用 `complete` 时只验证原库存，不刷新时间或哈希。实际再次执行必须显式重新激活该阶段。

## 版本与恢复

运行器修复会改变控制器或 Schema 指纹。旧 run 的锁、状态、原始输入和已完成阶段应原样保留，不能手动改锁使新代码冒充旧版本。归档原控制器，使用适用的已验证恢复或迁移路径，明确配置、累计预算、可复用检查点及其验证方式。已授权范围内继续；需要新的权限、扩大预算或无法确定的业务决策时，再向用户说明。旧版未冻结计划若没有经过认证的恢复记录，不能静默切换到 v7；现有迁移过的 v7 计划跨 run 复用仍需要可移植的认证来源，当前会阻断。

对于已终止、`plan_not_passed` 且尚无目标评测的优化 run，控制器支持受认证的多次跨版本计划恢复链；针对已认证的决议读取错误、未新增调用且同样没有目标评测的 `planning_error` 也支持继承耗时后恢复：从安装目录 `scripts` 运行 `python -m refiner.refine_plan_recovery --source-run <上一次run> --controller-archive <该run对应的旧控制器归档> --ancestor-run <最初历史run> --output <新的空路径>`。迁移逐代校验来源、调用记录、阶段证据和累计用量，保留未通过计划及覆盖缺口；它只复用已验证输入，不把失败计划标记通过。该路径不支持已有目标评测的 run；缺失证据或无法唯一关联历史时停止。
