# AI Task Orchestration 2.0

基线1daa1c7；分支feature/calendar-ai-task-orchestration。保留JSON Schema9、ChatJournal SQLite结构、A1字段策略、A2服务、原混合Undo栈；不实施A3/A4或SQLite迁移。

## 根因与范围

原工具只有单对象提案，Service预检拒绝多个写工具，无法表达一次有界批量事务；bulk接口只改已有对象，不能创建一批。会话只有selected/candidates，没有批次有序结果。删除“删了”不在原意图白名单；澄清后deleted字段未直接推进删除预览。send_message还会补回已终结pending操作，UI每轮重复render_operation。

## 原子批量和工具

CalendarEventService.preview_create_many最多10项，要求独立ID且不存在，逐项复用preview_upsert后组成原BatchPreview；提交使用原commit_bulk，一次Store保存、一个Undo。没有改写EventCommandHistory。

白名单新增batch_create_events、batch_update_events、batch_set_completion；每个是一个写提案，不能与另一个写工具同批执行。批量修改/完成只允许本会话已查得或最近任务实际生成的Event ID；未知/重复目标拒绝。Habit批量和批量删除没有工具。输入仍受Tool Schema、A1与语言字段scope校验。执行仍经Gateway.dispatch回Tk所属线程。

## Task Plan

calendar_task_plan.py 使用现有加密ChatJournal的kind=task记录，不改变SQLite表。字段包括task_id/operation_id、session_id、原request_id、source、operation、constraints、clarified、event_ids、expected、status、result/error_code。未写入前全批准备；ready→executing→completed。校验失败failed，歧义needs_clarification，后续同类型工具合并澄清参数并保持task_id；取消未执行任务标记cancelled。

执行前保存稳定ID、已选颜色和完整expected候选。任务completed记录实际Gateway详情；另写审计。session保存active_task和recent_task（ID与日期结果）。请求幂等沿用原request receipt：同request_id返回旧响应，文本相同但新request_id可以是新任务；“继续添加”在最近任务已完成时不会无依据再创建。

JSON和ChatJournal不是共同ACID事务。executing状态不盲目重放；get_task只读观察所有对象与expected是否相同，匹配才恢复completed，不匹配保留execution_uncertain。请求回执尚无response时也可观察关联任务；已经返回失败回执的重试保留原失败，可用服务get_task核查。该查询是Python能力，本轮没有增加独立HTTP任务查询路由；HTTP消息响应包含task，普通端与桌面使用同一服务。

## 上下文、颜色与删除

recent_task按实际日期/ID稳定排序；第二天/第二个/最后一个绑定稳定ID，作为目标锁传给后续查询/工具。批量工具也能消费最近整批ID。自然语言解释主要仍由模型完成，本地不声称是完整中文时间解析器。

“其他颜色”在明确序号目标时本地从现有palette排除当前有效色选择。批量不同颜色约束由本地抽样，最多调色板现有6种互异色；超出则澄清，不编造颜色。expected中固定选择，重试不重新执行抽样。对案例“未来/往后三天各一个”在本地验证从明天起三个日期；测试固定2026-11-14，期望15/16/17。连续多日但未明确独立事项时拒绝混用批量工具。

删除新增“删了”识别，保留delete_intent和awaiting_fields。用户选定目标后直接推进preview_delete，不重新问删除还是未完成。执行仍须原确认按钮或明确确认消息，保留TTL/revision/冲突/重复确认保护；删除成功清意图。没有无确认批量删除。

## UI与测试

send_message不把已终结操作补回新回合；UI只render pending卡片。批量结果列出实际日期、标题、颜色与完成状态，不显示模型伪造成功。

新增test_calendar_task_plan.py十三项：创建/日期/颜色/独立ID、保存/Undo、请求幂等、第二天、其他颜色、批量更新/完成、失败回滚、无依据继续不重复、未知/重复ID、恢复、删除简写与卡片、非法成员、多日对象不拆分、回执失败、澄清同ID、真实loopback HTTP（部分用例合并多断言）。完整结果见交付报告。

最终完整测试950/950通过（原937+新增13）；Python127文件编译及git diff --check通过。提交前再次完整运行通过。终结审计写入失败时保留原执行意图和completed任务回执，响应audit_pending，不误报“未修改”或重放批次。

Windows tools/check_calendar_chat.py：四主题×两视图8组，实际192DPI，Mock LLM、loopback_http、modal_search_undo通过，errors=[]。tools/check_interactions.py四主题Compact/Flow/Timeline拖拽/Resize/编辑/Undo/Modal通过。未执行真实DeepSeek或正式EXE构建，不保证模型每次都主动选新批量工具；错误计划仍受本地预检和有界重规划限制。

限制：真正跨存储exactly-once、通用时间范围解析、批量仅预览确认UI、超出6种互异颜色、Habit批量、批量删除均未开放。旧单事项提示中的内部单对象限制仍指单对象工具，批量需用一个专用任务工具。任务无分页/独立管理窗口，沿用Journal容量保护，不自动清理安全回执。
