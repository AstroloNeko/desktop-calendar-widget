# Stage A2：统一 Event Application Service

基线09e29ea；分支refactor/calendar-operation-services。本阶段不实施Repository、SQLite、账户、A3/A4或数据格式变更。

## 服务与接口

calendar_event_service.py 的 CalendarEventService 包装现有 EventCommandHistory；event_service(history)创建无状态薄facade，不创建历史栈。OperationContext仅source/request_id，不是身份授权。服务不依赖Tk、网络、聊天会话或窗口。

预览：preview_upsert、preview_update、preview_patch、preview_schedule、preview_bulk；提交：commit、upsert、update、patch、set_completed、create_quick、delete、bulk。patch使用A1显式字段；update保留内部命令字段用于提醒稍后等现有协调。取消预览只丢弃预览，没有写入方法。批量commit调用原commit_bulk，不逐条保存。

EventOperationResult包含operation、context、原EventChange/BatchChange、Event ID、提交后不可变EventState快照；提供succeeded/changed/affected_dates/ddl_changed/reminders_changed/count。无变化返回成功但changed=false。失败抛EventOperationError，区分validation_failed、event_not_found、operation_conflict、incompatible_data、storage_unavailable、invalid_preview。删除不存在对象保持原命令的无变化语义。

快照暂复用history._capture内部接口，这是当前命令层依赖，未声称已完成Repository解耦。reminders_changed保守等于changed，与原UI失效策略一致。提交结果不包含刷新成功与否；没有事件总线。

## 四类接入

- app.py：编辑器保存最终upsert、快速添加、完成、删除、推迟/稍后、排期提交通过服务；_run_event_command解包result.change，再调用原局部刷新。
- bulk_ui.py：bulk通过服务，原选择、确认和反馈保留。
- global_drag_ui.py：ScheduleDrag接收服务的预览接口；鼠标释放沿用app.commit_event_preview进入服务提交。Flow/Timeline不改规则和绘制。
- CalendarActionGateway：Event预览和执行调用服务；Habit仍使用原commands。所有访问仍在TkDispatcher中。先记录executing、提交后记录succeeded、再refresh；刷新失败不能导致写入重放。HTTP、AI确认/自动执行、幂等和ChatJournal结构未修改。

## 混合历史与事务

Undo/Redo刻意不放进Event-only服务：app上层直接协调唯一EventCommandHistory，允许RoutineChange，并沿用原_refresh_event_change的Habit分支。Habit新增/修改入栈与Event/批量顺序不变；未修改event_commands.py。

服务预览不保存/入栈/发通知；提交重新检查原命令冲突。Store.persist_event_states/save和回滚保持原样，单操作与整批均一次保存；重复设完成无变化且不保存。服务没有UI回调。JSON仍无跨进程revision事务，ChatJournal与JSON仍是不同提交，不能声称exactly-once数据库事务。

## 验证

A1基线927/927通过。新增test_calendar_event_service.py十项覆盖CRUD、完成无变化、零写预览、保存次数、结果日期、冲突、非法Patch/ID、失败回滚、批量一次Undo、混合Habit历史、无时间DDL。完整937/937通过；两个UI mock改为模拟服务边界，原断言保留。Python125文件compile及diff check通过。

Windows GUI：四主题Compact/Flow/Timeline、拖动/Resize、编辑保存、Undo、搜索与Modal通过，实际192DPI。Chat GUI四主题×两视图8组通过，loopback_http与modal_search_undo通过，errors=[]（build/calendar-chat-fix/a2-source.json，忽略的测试产物）。所有测试为临时Store/Mock LLM，无真实用户数据变更。未构建正式EXE、未做长期运行资源试验；无新增工作线程、无新增持久化或调度循环。

A3前置：保持本服务回归，单独审计Habit删除/打卡及分类联动事务，先补失败回滚测试，再评估接入共享命令；不要混入SQLite迁移或改动窗口机制。
