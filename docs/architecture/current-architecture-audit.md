# 当前架构审计

审计基线：`feature/calendar-chat-service-phase1`，`f7e622c10dbe814046b027d3944a3761e1bc6a8f`。本轮只写设计文档；以下文件、类和方法均指该提交。入口见[迁移路线](architecture-migration-roadmap.md)。

## 1. 已核实的现场

- 工作区开始时干净；AI 创建/修改 Habit 已提交，不是未完成的实验补丁。
- `version.py` 为 **0.10.2**；这不是远端 Release 状态的证明，本轮未访问发布服务。
- `data_compat.py::DATA_SCHEMA_VERSION` 为 **9**；`data_backup.py::FORMAT_VERSION` 为 **1**；Chat SQLite `user_version` 为 **1**；HTTP `/v1/health` 报 API 1。它们不是同一个版本号。
- `app.py` 约 **7,600 行**，协调 UI、窗口生命周期、编辑器、渲染、命令和提醒；并非所有业务都还在这个文件。
- `tests/` 有 **42 个 test_*.py 文件**。基线记录 915 项，本轮执行结果见路线文档。

## 2. 实际存储：不是“已经全部 SQLite”

| 数据 | 真实位置/实现 | 范围 |
|---|---|---|
| 日历核心 | `calendar_core.py::Store`；默认 `%APPDATA%/DesktopCalendar/calendar_data.json`，可注入 data_file | events/categories/routines/date_states/settings/notified，全文件 JSON |
| 会话、请求回执、Pending Operation、AI 审计 | `calendar_chat_sessions.py::ChatJournal`；数据目录下 `ai-assistant/sessions.sqlite3` | 单表 records(kind,id,owner,payload)，payload 为当前 Windows 用户 DPAPI 密文；WAL、FULL synchronous |
| API Key、本机 Token、模型配置与同意状态 | `llm_provider.py::AIConfiguration`；`ai-assistant/configuration.dpapi` | 设备私密配置，不是日历数据 |
| 手动/自动备份 | `data_backup.py`；`.dcalbackup` | 仅 manifest.json + calendar_data.json；**不包含 Chat SQLite 和 AI 凭据** |
| Undo/Redo、拖拽候选、视图选择 | `EventCommandHistory`、`ScheduleDrag`、UI | 当前进程内存；不跨重启 |

Store.save：先检查 load_error/磁盘 Schema，迁移前建保护备份，再写同目录 `.tmp` 并 replace。单文件替换不等于跨进程事务：它没有整个“读—改—写”区间的并发版本锁；Schema 相同的旧内存仍可能覆盖另一个实例的新内容。不能将现有 Schema gate 当作乐观锁。

ChatJournal 每次操作新建连接、RLock 串行访问，records 主键(kind,id)，owner 验证。它并非 Event Repository，没有规范化事项表。日历 JSON 写入与 Chat 意图/结果记录是两个提交：gateway 先记录 executing，再提交业务，再记 succeeded；异常后观察状态，**不自动重放不确定写入**。这是必须保留的安全性，但不是 ACID exactly-once。

## 3. 依赖与职责

```text
Tk 编辑器/快速添加/拖拽/批量
  -> CalendarApp 命令入口 -> EventCommandHistory -> Store -> JSON
桌面 Chat / LocalChatHTTP -> CalendarChatService -> CalendarActionGateway
  -> TkDispatcher -> 同一个 EventCommandHistory -> Store
                 -> ChatJournal(SQLite)，另一个持久化边界
Store -> TimelineMonth -> Flow layout / Timeline UI
Store -> ReminderEngine -> Windows notification adapter
```

| 能力与代码 | A 规则 | B 应用流程 | C 持久化 / D 平台耦合 |
|---|---|---|---|
| `calendar_core.Event` / Store | 展开、类型、颜色、DDL | Store 同时承担 CRUD | Store 直接 JSON；core 还 import ui_theme 做主题设置归一化 |
| EventCategory | 继承/覆盖、删除冻结颜色 | Store.create/upsert/delete_category | 直接 save；GUI 回调做空名校验、刷新、清 Undo |
| `RoutineItem` / `habit_schedule.py` | 周期、打卡、规则历史 | prepare/persist_routine、progress | 同 JSON；RoutineEditor 组装对象，删除/打卡仍直达 Store |
| DDL、多日、skip-day | Store.event_dates/end_date/is_workday、complete_ddl_groups | 同一逻辑事项派生日期，非每天实体 | 自定义 leave/holiday 存 date_states；法定规则在 holiday_data.py |
| Reminder | reminder_engine.py 计划、指纹、补偿/稍后；Store.due_routine_reminders | ReminderEngine 保留发送状态，app 定时与生命周期 | settings 内原生提醒配置/ledger；notified 是另一个旧提醒账本；Windows 发送在适配器 |
| Undo/批量 | event_commands.py preview/commit/bulk | 一个命令一次发布、冲突检查、回滚 | 直接依赖 Store 列表、通知键和 persist_*；非 Tk 但不是 Repository 无关 |
| AI | tools 严格 schema；attributes/intent/habit_gateway 语言约束 | Service 编排、Pending/幂等；Gateway 预览执行 | Gateway 同文件含 TkDispatcher；默认 dispatch 可测试，生产由 ChatRuntime 注入 Tk |
| Flow / Timeline | timeline_model.py 月份 clipping/segments；calendar_flow_layout.py 跨周排布 | schedule_drag.py 只预览，释放提交 | global_drag_ui.py / app.py 使用 Canvas；不可把像素命中算作领域规则 |
| Search | event_search.py 构建索引和查询 | UI 搜索任务协调 | 不应让未来 REST 直接依赖搜索浮窗 |

这些核心算法没有直接访问 Tk Widget；但 `calendar_core -> ui_theme` 的导入边界尚不纯净。`llm_provider.UserProtection` 是 Windows 专属，ChatRuntime 定义在 `calendar_chat_ui.py` 并读取 app 生命周期；不能直接 import 整个 UI 当无头 FastAPI 启动器。

## 4. 写入路径与具体耦合点

1. **Event GUI**：`EventEditor.save` 从 Tk 变量读取、校验标题/日期/持续长度并重建 Event → `CalendarApp.upsert_event` → `_run_event_command` → history.upsert → preview/commit → Store.persist_event_states。快速添加、完成、删除同一命令入口。
2. **拖拽/批量**：`ScheduleDrag` → history.preview_schedule；app.commit_event_preview 最终提交。history.preview_bulk/commit_bulk 一批一次 Store.save。值得保留，不重写为逐项 REST 循环。
3. **AI Event**：Gateway._patch/prepare_event 对 DTO 另做校验，语言明确性由 attributes/intent 决定。与 GUI 日期、颜色、持续范围校验有重复；语言猜测不能搬进普通业务 API。
4. **Habit 编辑**：RoutineEditor.save → history.upsert_routine，与 AI 共用；但 UI `RoutineEditor.delete`、`CalendarApp.toggle_routine` 直调 Store.delete_routine/toggle_routine。不要声称 Habit 全部 CRUD 都已接入 Undo。
5. **分类**：CategoryEditor.save → Store.upsert_category；manager 删除 → Store.delete_category；随后 `category_data_changed` 清历史。多个 Event 的分类/颜色被联动改变，但目前不是同一套可逆命令。
6. **日期状态**：单日详情调用 set_date_status；app 范围状态循环逐日调用，可能一轮多次全量保存。属于下一个原子命令的真实切入点。
7. **设置/提醒**：app 多处直接改 settings 再 save，ReminderEngine 也保存状态。未来不能只替换 Event 保存就宣称数据库迁移完成。

Store.persist_event_states 和 persist_routine 有内存回滚；分类 CRUD、直接 delete 等路径没有相同显式回滚保障。应补故障注入测试后统一事务，而非在此审计中顺手改写。

## 5. 日期与历史的重要兼容事实

- 每个多日 Event 是**一个稳定 ID + duration_days**，Timeline.source_task_ids=(event.id,)，无需用标题合并或新建 Series。skip-day 由 Store.is_workday 注入，不能只用 Event 默认 system_is_workday 忽略用户请假。
- native ddl 和 end_as_ddl 都取实际最后有效日；urgent 本身不进入 DDL。所有视图应消费派生结果。
- `app.parse_event_due`、Store.make_quick_event、Gateway._patch 的无时间值用 **23:59 + has_time=False**。这是旧表示法，未来 DTO 必须 local_time=null；DDL 的旧截止边界仍需独立保持，不能迁移时变成 00:00。
- Habit history 用 `until` 排他边界保存旧规则；变更当日起新规则，同日保留首次旧规则；打卡列表独立。`rule_on` + Store.routine_progress 已保护过去规则，但 holiday/leave 日历仍实时引用；更早未记录的规则不能重建。
- created_at 为无时区本地 ISO；Habit.created_on 只有日期。不可伪装成精确 UTC 历史时间。

## 6. 刷新与剩余风险

EventChange/BatchChange 已有 affected_dates、ddl_changed。`_refresh_event_change` 局部刷新 Compact，Global 必要时重画活动 Canvas，并非每次重建整窗。Habit 分支和 ChatRuntime.refresh_habits 仍 render 全视图；分类/恢复也较宽。未来 ChangeSet 扩展类型化影响范围即可，不需要事件总线框架。

优先风险：共享校验缺口；多种 save 路径；JSON 与 AI 回执跨存储提交；读取时跳过部分非法旧记录（严格备份校验更强）；时间含义不明确；DPAPI 不能跨设备直接解密；Undo 不能不经冲突检查回放到共享日历。以上是代码静态证据推导的改造风险，不是本轮已复现的数据损坏报告。
