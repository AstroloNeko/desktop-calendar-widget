# Stage A3：Routine、Category 与日期状态事务

基线 `3ba63b1`（包含 A1/A2、AI Task Orchestration 和两级批量删除），
分支 `refactor/routine-category-transactions`。没有合并或丢弃其它分支成果。
JSON Schema 9、备份格式 1、数据目录、版本号、ChatJournal 结构均不变。

## 真实路径与缺口

```text
原 RoutineEditor.save / AI Gateway -> EventCommandHistory -> persist_routine
原 RoutineEditor.delete -> Store.delete_routine -> save（无本地回滚）
原 toggle_routine -> 原对象打卡数组修改 -> upsert_routine（修改已泄露到原对象）
原 CategoryEditor / Manager -> Store -> 原对象颜色/分类变更 -> save（无回滚）
原 Flow 日期范围 -> 每日 set_date_status -> 每日 save

现在 GUI / AI Gateway -> CalendarMutationService -> 原命令或 Store._commit_fields
                    -> 一次 JSON save -> MutationResult -> 上层刷新
```

`calendar_mutation_service.py` 的无 Tk 服务复用 A2 OperationContext、错误映射和
同一个 EventCommandHistory。接口为 `upsert_routine`、`delete_routine`、
`set_routine_completed`、`upsert_category`、`delete_category`、`set_date_status`。
MutationResult 包含 changed/succeeded、operation、routine/category/event IDs、
affected_dates、ddl_changed、reminders_changed、calendar_rules_changed、原命令 change。
未添加 Repository、事件总线、第二套命令历史或新的语言规则。

## 持久化边界

`Store._commit_fields` 接受已经构造好的受影响字段候选。列表仅复制结构，
分类联动只复制相关 Event；Habit 只复制当前对象（包含打卡与规则历史），
日期状态复制小型覆盖字典。不是为每次操作备份整个数据库。

无变化零保存；发生变化只调用一次既有 `Store.save`，继续同目录临时文件替换。
失败且磁盘摘要未变化时恢复原字段引用和旧 notified。成功后才同步旧 UI
持有的 Event/打卡引用，避免成功后悬空显示，同时避免失败前污染原对象。

额外流式读取提交前后文件摘要用于异常判别，不将整库快照加入 Undo。
若异常时文件已变化或无法核实，抛 MutationUncertainError，保留候选内存、
设置 load_error 阻止后续保存，服务映射 execution_uncertain；要求重新加载核查，
不报告已回滚，也不自动重试。Habit 命令层及 AI Gateway 不再用旧浅快照覆盖
此不确定结果。真正失败仍保留旧 Undo 栈；未知提交不新增 Undo。

这仅保证受控单进程事务边界。不能防止另一进程在摘要检查之间写入，
不是 SQLite ACID，不是 JSON/ChatJournal 跨存储 exactly-once。

## Habit

创建/修改仍调用 history.upsert_routine，原 prepare_routine/preserve_schedule_history、
until 排他边界、同日修改、锚点、周期算法均未改。persist_routine 使用同一局部提交
辅助机制。失败恢复属性、历史、打卡及本次受影响的旧提醒键。

删除一次提交移除 Habit 和其旧通知键；保持原来的不可 Undo 删除语义。
打卡服务接收明确 completed 布尔值，相同状态无变化；旧 toggle 包装器读取
当前真实状态后委派，保存成功才同步调用者旧引用。非计划日限制和 legacy todo
语义不变。打卡继续不入 Undo；原 Habit 属性 Undo 会保留之后的打卡记录。
Event/Habit 共享栈与入栈顺序不变。

AI Gateway 在已有 TkDispatcher 内使用此服务，未改工具、自动执行、确认、
会话或 HTTP 路由；业务提交之后才写结果回执和刷新。

## Category

保留名称大小写无关唯一性、模型颜色默认/排序规则。候选在保存前验证。
改色同步继承色，override 不变。删除先在原分类仍存在时计算有效色，
构造所有关联 Event 的无分类/override 候选，与分类删除一起保存一次。
ID、日期、DDL、提醒、备注、完成等字段不变。
成功后服务清除既有共享命令历史，维持原 category_data_changed 的历史失效保护；
失败不清历史。没有新建分类 Undo 系统。

## Date State

单日和范围共同调用 prepare_date_status_range / set_date_status_range。
非法日期、逆序范围、未知状态拒绝且零写入；读取旧数据仍沿用原规范化函数。
normal 只删除用户覆盖，法定日历不变。范围一次保存，无变化零保存。
服务用原 Store.event_dates 在暂时注入候选覆盖后计算排期，finally 恢复；
此过程仅允许串行应用线程，无保存或通知。排期不可计算时提交前拒绝。
提交结果包含直接日期及 skip-day 事项变化前后全部实际日期（含范围外 DDL）。
所有 Habit 标记需重算，因为历史规则也可能引用工作日。

## 刷新与设备状态

app._run_aux_mutation 将业务提交与刷新异常隔离。提交失败只显示错误，不调用
成功刷新；刷新失败仍返回业务成功并提示数据已保存，用户不应重复提交。
单日控件失败时恢复显示原状态。Routine、日期事务成功后调用原 reminder
invalidation / render / RoutineManager.refresh，日期额外使搜索失效。
render 重建当前活动视图，包含 DDL、Flow/Timeline 和详情；未改窗口机制。
MutationResult 暴露未来更窄刷新的信息，本阶段保守保留原宽刷新策略。
原 native_reminder_state 投递账本不清空；旧 notified 键只按原规则处理。

## 验证

- 基线 965/965（28.853 秒）；新增 test_calendar_mutation_service.py **32 项**。
- 最终完整 **997/997**（28.952 秒），涵盖 Habit、Category、日期、A1/A2、
  EventCommandHistory、Chat、备份恢复及已有窗口测试。
- 故障注入覆盖创建/修改/删除/打卡/取消/规则历史/提醒、分类关联回滚、
  日期范围、磁盘替换后异常、旧引用、无变化、单次保存、混合 Undo、
  UI 刷新失败不重放、备份恢复及 native 账本保留。
- 133 个 Python 文件 compile 通过；git diff --check 通过，无冲突标记。
- tools/check_mutation_transactions.py：真实 Tk 四主题×Compact/Global **8 组**，
  创建/修改/删除/打卡、分类、范围一次保存、Undo/Redo、grab 释放通过。
- tools/check_interactions.py：四主题 Compact/Flow/Timeline、拖动/Resize、
  原编辑器、Undo、搜索单实例/Modal 通过。
- tools/check_calendar_chat.py：四主题×两视图 8 组通过，Mock LLM、
  loopback_http / modal_search_undo 通过，errors=[]；报告在忽略的
  build/calendar-chat-fix/a3-source.json，不纳入提交。
- 实际 Windows 192 DPI（200%）；未将模拟 DPI 视为真实多显示器验收。
- 所有验证使用临时 Store；未启动真实模型、未改用户数据、未构建正式 EXE。

## A4 前置与限制

下一阶段保持上述服务契约，拆 TkDispatcher/运行时组装而不更换规则。
Store 仍是公开可变对象；直接修改 canonical 对象再调用保存不属于服务候选
契约，应由未来 Repository 边界进一步收紧。分类失效 Undo、打卡/删除不可
Undo 保持原语义。日期规则预览目前依赖串行线程，未来并发适配需显式规则
上下文。摘要只帮助识别不确定保存，不能替代数据库事务与多进程版本锁。
本阶段停止，不实施 A4 或 SQLite 迁移。
