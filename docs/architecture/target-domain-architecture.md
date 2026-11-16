# 目标业务分层（渐进方案，不是本轮实现）

基于 [当前审计](current-architecture-audit.md)。保留成熟算法和命令，不新建第二套 Event/Habit/recurrence，不为层次增加空抽象类。

## 1. 目标依赖

```text
Tk / Chat / Local HTTP / 未来 FastAPI
                  ↓
Application Services + DTO + OperationContext
          ↓                    ↓
Domain rules             Repository / UnitOfWork port
                               ↓
                      JSON adapter → SQLite adapter
提交后的 ChangeSet → Desktop refresh / Reminder invalidation
```

Domain 不 import UI、SQLite、网络；Application 不访问 Widget。Repository 不判断“用户说的绿色是否明确”；Adapter 不直接写数据库。Flutter 是 API 客户端，不直接共享 Python 内存对象或 SQLite 文件。

## 2. 复用与最小抽取清单

| 现有符号 | 建议归属/动作 | 不改变的约束 |
|---|---|---|
| Event/EventCategory/RoutineItem | 先保留原 import 兼容出口，逐步移 domain models | ID、默认值、旧解析行为；迁移严格校验另入口 |
| habit_schedule.*、holiday_data.is_workday | 直接复用 | 无效月日跳过、锚点、历史排他边界 |
| Store.event_dates/is_workday/event_end_date | CalendarRules，显式日历规则上下文 | 用户 leave 优先于工作日、调休保留 |
| Store.effective_event_color、类别删除冻结 | 分类领域规则 + 应用事务 | 一事项一分类、override 不随分类变色 |
| Store.complete_ddl_groups | 查询投影服务 | 末日唯一、已完成沉底、24小时界限不变 |
| EventCommandHistory.preview_* / commit / bulk | 当前 Application 起点；逐步注入仓储 | 预览零写、一次释放一事务、Undo 同栈 |
| EventEditor.save + Gateway._patch | 提取显式 EventDraft/EventPatch 验证与 candidate 构建 | GUI 默认值与 AI 明确性策略分别位于适配层 |
| RoutineEditor.save + prepare_habit | 共用 HabitPatch 业务验证；语言 scope 仍留 AI | 历史/ID不变、多字段一次提交 |
| TkDispatcher / ChatRuntime | 分离 Tk 线程桥与无头 service factory | 当前线程队列、退出/Modal阻断策略不变 |
| Store.load/save、data_backup | JSON Repository 与 BackupService | 旧文件只读兼容、Schema fail closed |
| TimelineMonth/FlowLayout | 只读投影/布局 | 不创建“CalendarTask”；实际日期/颜色同源 |

`calendar_core` 中 theme 设置归一化留给 DesktopPreferences 适配层；事项颜色是用户领域数据，仍应保留，不要把用户色板当 UI token 删除。

## 3. Application 最小接口草案

```python
# 签名设计；本轮不新增可执行类
preview_event(context, event_id, patch, expected_revision) -> EventPreview
commit_event(context, preview, idempotency_key) -> MutationResult
update_habit(context, habit_id, patch, expected_revision, idempotency_key) -> MutationResult
set_habit_completion(context, habit_id, day, completed, idempotency_key) -> MutationResult
change_category(context, category_id, patch, expected_revision) -> MutationResult
delete_category(context, category_id, expected_revision) -> MutationResult
apply_batch(context, ids, operation, expected_revisions, idempotency_key) -> MutationResult
```

MutationResult = committed对象/删除身份 + revision + ChangeSet + operation_id；无变化返回 changed=false。只有持久化成功才报告成功。错误类型至少 Validation、NotFound、Conflict、IncompatibleData、StorageUnavailable、Forbidden。

OperationContext 当前由本机适配器产生 local principal、source、request_id、clock、calendar_id；不是客户端 owner_id。未来服务端授权插入此边界，不把账户系统塞进当前 Tk callbacks。

Patch 保留未提供/null/值三态。API 使用明确 completed=true/false，不暴露 toggle 给可重试请求。批量整批预验证、一次提交；AI 单对象限制仍是 AI 策略，不随普通批量 API 的存在而放开。

## 4. 事务、Undo 和通知

Repository 先用窄能力接口：get_event/list_events、get_routine、list_categories、读日历规则、apply_changes(expected_versions)、读写偏好。不要直接返回可任意修改的内部列表；查询给不可变/脱离存储快照。JSON 初版 adapter 可包现有 Store，在接口稳定前不改格式。

事务顺序：校验/授权 → 读取版本 → candidate/实际日期计算 → 原子保存相关状态 → 增加 Undo 命令 → 发 ChangeSet。UI 刷新失败不能回滚已向调用者承诺的事务或触发重放；提醒 invalidation 为可重试提交后动作。

ChangeSet 建议 event_ids、habit_ids、category_ids、affected_dates、ddl_changed、reminders_changed、calendar_rules_revision。规则变化可能影响多个未来事项，需使 preview/cache 失效，不能只用 Event revision 判定有效。

Undo 保留单对象/整批快照，不备份整库。共享日历阶段 Undo 是带 expected_revision 的补偿操作，不把数据库回滚到全局旧时间点。Habit Undo 保留后续新增打卡，已有行为必须继续测试；删除/打卡是否纳入同栈独立小步扩展。

## 5. 时钟/线程/平台边界

- Clock 注入 local today 与 UTC now；CalendarTimeZone 显式 IANA。日期规则不读取 UI 选中日期作为隐式全局状态。
- Tk 当前仍是数据操作所属线程，服务调用经 TkDispatcher；未来无头进程改为服务自己的串行执行器/数据库事务，不携带 app 引用。
- HTTP/LLM 工作线程不能访问 Tk 或共享 sqlite3 connection；禁止持事务等待模型网络返回。
- ReminderEngine 计划规则保留，发送依赖 NotificationBackend；Windows 托盘、DPI、Z-order 留 Desktop adapter。未来 Android/iOS 通知投递单独适配。
- DPAPI 是 SecretStore 的 Windows实现；跨平台提供系统钥匙串实现，绝不明文回退。

## 6. 第一项实际重构

建议首先提取 **Event 输入验证与局部 Patch candidate 构建**，覆盖 EventEditor.save、parse_event_due、CalendarActionGateway._patch；不同时动 JSON/HTTP/窗口结构。用现有命令 preview/commit 作为稳定终点。

先补“GUI与AI同一显式输入产出同一candidate；仅title修改其它字段不变；无时间仍保留旧DDL边界”的契约测试。自然语言推断/自动执行决策不下移；读取旧文件的宽容归一化与新输入的严格校验也不合并。这样第一步可独立审查并为后续 Repository 接口提供真实需求。
