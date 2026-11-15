# AI 批量删除与“全部”澄清修复

基线94ba7ef；继续feature/calendar-ai-task-orchestration。仅AI删除/查询/确认相关改动，不实施A3/A4、不修改JSON Schema9、Store格式、HTTP路由或EventCommandHistory。

## 实际根因及证据限制

原白名单只有preview_delete(event_id)。多个候选时_tool将未选定目标返回单选澄清，_conversation没有“全部”绑定集合的分支。Orchestration2.0虽有批量创建/修改/完成，但没有批量删除。继续向单选工具传一个ID无法表达用户授权删除集合，靠Prompt重试会循环。

原query_events使用EventSearchIndex模糊查询；keyword未统一剥离常见外层引号，也不能从模型参数自动证明“这几天”的可信日期范围。用户历史那次0/4没有原始tool参数日志，不能断言一定由引号导致；可能参数/范围差异，本轮未读取真实用户数据或伪造实际模型日志。现在删除查询有专用exact-title路径，并记录query_mode、keyword_ref摘要、date_range、total，便于脱敏复现，不记原始事项文本或Token。

## 搜索与上下文

明确“全部删除”保存batch_delete_intent、精确标题、可信范围；支持普通/中英文/不配对外层引号剥离、外层空白。不使用模糊substring将WB实验后处理纳入WB实验。无范围的“这几天”询问起止ISO日期，不能自动查全历史；优先已有明确查询范围或最近任务范围。候选按日期/ID排序，实际执行日期交集复用Store；同名同日仍保留所有不同ID。

delete_scope保留clarification_id、intent、完整ID、total/truncated、过滤条件、每对象完整状态hash和15分钟到期时间。“全部”直接选择该集合，绕过LLM重搜/单选；超过10或不完整展示拒绝，要求缩小范围。重复“全部”返回同一Pending身份和阶段，不重置确认，不重复创建。没有候选上下文不会选择整个日历。

## 两级确认状态机

新增白名单delete_events_batch，仅准备清单。原单删除在明确批量意图下重定向完整集合，不允许模型选第一条代替全部。

calendar_batch_delete.py复用现有operation记录，preview.object_type=batch_delete；包含event_ids、完整展示行、hashes、clarification_id。阶段：reviewing_batch → awaiting_final_confirmation → executing → completed，另有cancelled/expired/conflict/failed/uncertain。

第一级按钮“删除这N项”只更新阶段并旋转revision，零写入/零Undo；第二级“最终确认删除”才执行。两级均须当前revision；文本“确认”不携带revision不能跨过按钮校验。同revision重试不能推进新阶段，终结操作重复确认只返回原结果。用户可任一级取消。

两次确认均重新核对绑定快照；变化则整批冲突，不偷偷选新候选，也不删除未变的部分。需要用户重新查询、核对后重新确认。清单全量显示标题、日期、时间及完整ID，同日同名可区分。完成/取消清理删除上下文，不劫持下一条普通查询。终结卡片不重新成为pending。

## 执行、回执和恢复

日历访问仍Gateway.dispatch/TkDispatcher；执行调用A2.events.preview_bulk(ids,'delete')和commit，底层原commit_bulk/persist_event_states，一次save、一条共享Undo；未循环delete、未建新历史。失败沿用整体回滚。

先写executing意图，再提交，再记succeeded，最后刷新。结果包含所有ID/数量/operation_id；审计关联完整集合和阶段。UI失败不重放；回执失败留executing，之后只观察所有ID是否已不存在，匹配才记录恢复成功，否则uncertain，禁止自动执行。JSON和SQLite仍无共同事务；观察一致不等于证明跨存储exactly-once。终结审计I/O失败保留durable operation回执并标audit_pending。

## 测试与范围

tests/test_calendar_batch_delete.py将失败对话作为回归序列，合成A/B同日同名、C/D其他日期，E细胞实验、F组会；覆盖全集选择、三种引号、精确排除其它实验、范围澄清、两个revision、取消/过期、一次保存与Undo原ID、重复确认、冲突与故障回滚、会话隔离、恢复不重放、混合Habit历史、HTTP两级确认、删除后正常查询。

tools/check_batch_delete.py真实Tk四主题×Compact/Global共8组通过，192DPI；核对实际按钮、完整ID清单、第一级无写入、第二级删除、一次Undo和grab释放。既有check_calendar_chat.py八组Mock聊天/HTTP/Modal通过；check_interactions.py四主题Compact/Flow/Timeline通过。

最终自动测试数量与提交见交付报告；编译和diff检查执行。测试全为隔离Store，不访问真实日历。未执行真实DeepSeek或正式EXE构建；不是完整中文时间解析器，无法可靠解析的日期范围需要澄清。批量上限10，超限不自动拆批。后续Task Plan创建/修改机制及单项删除确认保持原行为。

完整965/965通过（新增15项）；130个Python文件compile通过，git diff --check通过。历史0/4问题未调用真实模型复现，已用确定性隔离fixture验证带/不带引号的删除范围一致。
