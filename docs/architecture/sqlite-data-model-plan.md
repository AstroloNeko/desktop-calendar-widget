# SQLite 数据标准化与迁移计划

状态：设计草案，未执行迁移。基线见[当前审计](current-architecture-audit.md)。核心仍为 JSON Schema 9；仅 ChatJournal 使用 SQLite。不要将其 records 密文表直接扩成日历数据库。

## 数据边界与候选表

建议核心 calendar.sqlite3 保存业务事实和业务操作回执。设备偏好、提醒投递账本可初期同库分表，但标明不参与未来同步。Chat 会话继续独立；凭据使用平台 SecretStore；Undo 历史留当前进程。

| 候选表 | 主键及主要字段 | 兼容要求 |
|---|---|---|
| schema_migrations | version, checksum, applied_at_utc | 不等于 App Version；PRAGMA user_version 作快速门禁 |
| calendars | id, name, timezone, revision | 初次迁移生成一次稳定个人日历 ID |
| events | id, calendar_id, title, start_date, local_time nullable, event_type, duration_days, skip_non_working_days, end_as_ddl, category_id nullable, color, color_mode, reminder_minutes nullable, notes, done, order_key, revision | 原 Event ID 原样保留，不生成每天一个实体 |
| categories | id, calendar_id, name, color, sort_order | 删除前事务内冻结关联事项有效色，不能只 SET NULL |
| routines | id, calendar_id, kind, title, color, created_on, completed_on, enabled, reminder_enabled, reminder_time, frequency, interval, month_day, year_month, start_on, end_on, workdays_only, order_key, revision | 保留旧 kind=todo，不只迁移 habit |
| routine_weekdays | routine_id + weekday 联合主键 | 0=周一，范围0..6 |
| habit_completions | routine_id + occurrence_date 联合主键 | 原 habit_done；不实例化未来计划 |
| habit_schedule_history | routine_id + until_date 联合主键，validated rule JSON | 保留 Schema9 排他边界和规则快照 |
| date_states | calendar_id + day 联合主键，state | leave/holiday；normal 移除覆盖，官方日历不被改写 |
| device_preferences | device_scope + key，value JSON | geometry/theme等；未知旧设置先保留 |
| reminder_preferences / reminder_delivery_state | device_scope、subject_id、plan_key、状态 | 区分用户策略与设备投递、重试、稍后记录；映射旧 notified 和 native_reminder_state |
| operation_receipts | principal_scope + idempotency_key，request_hash/result/status | 业务写入与回执同事务；Chat journal 关联回执 |

核心记录逐步加入 created_at_utc、updated_at_utc、revision。旧 Event.created_at 没有时区，保留 raw 值与来源；旧 Habit.created_on 只有日期，不能伪造精确创建时刻，可保留 nullable UTC 和 imported_at_utc。

每连接启用 Foreign Keys；category 与 Event 必须同 Calendar，以组合约束或事务校验保证。CHECK 持续1..365、间隔1..999、合法类型/颜色模式；日期仍需业务严格解析。列表顺序使用 order_key，不依赖 SQL 默认顺序。

索引起点：events(calendar_id,start_date,id)、events(calendar_id,category_id,done)、routines(calendar_id,enabled)、history(routine_id,until_date)、completions(routine_id,occurrence_date)。实际 DDL 日期受工作日覆盖影响，不把 start+duration 当永久正确索引；先选候选再用现有算法，性能不足才增加带日历规则版本的可重建投影。

## ID、共享与同步预留

新建路径使用 UUID4，适合跨设备。旧读取允许非 UUID ID，API 必须当不透明字符串接受；不得重新编号。重复导入使用来源命名空间/映射与冲突报告，不按标题合并。

未来 User → CalendarMembership → Calendar；Event/Habit/Category 属 Calendar。成员 Owner/Editor/Viewer；个人日历默认不共享，共享是主动选择。现阶段仅规划稳定本地 Calendar ID，不实现登录。服务器根据认证身份判断 membership，不能信任客户端 owner_id 或 Prompt。

共享 Habit 是“共同完成”还是“每人独立打卡”尚需产品决定；当前记录只有 Habit+日期，未来加 actor 时必须保留原历史含义。跨日历移动/分类映射作为独立操作，不隐式混合私人数据。

现在值得标准化：ID、归属、revision、时间语义与来源。暂缓 outbox/tombstone/sync cursor/CRDT/邀请；启动同步时删除必须有墓碑及保留策略，避免离线复活。冲突依赖 expected_revision 和显式合并，不采用 updated_at 最后写入覆盖。周期与打卡是不同冲突域。

## 日期与时间

- start_date、completion date、history boundary 为 YYYY-MM-DD，不转 UTC、不随旅行跨天。
- local_time 为 HH:MM 或 null。当前无时间事项的 23:59 + has_time=False 是兼容占位，不应暴露为用户设定时间。
- deadline_date 派生自最后有效日；迁移兼容层保留现有无时间 DDL 的23:59判断边界。普通纯日期事项不能自动获得 deadline。改为翌日零点排他边界属于另一个行为变更，不夹带迁移。
- 提醒实际触发点由日历 IANA 时区和当地日期时间解析为 UTC；DST 重复/不存在时刻需明确策略和测试。不能将旧 naive local wall time 直接加 Z。
- 新元数据使用 UTC RFC3339。旧数据源时区需明确确认，不从聊天所在地猜测。

## SQLite 事务、备份和恢复

标准库 sqlite3 足够。每线程/工作单元独立连接，不跨线程共享 Tk 连接；不持事务等待 LLM/网络。写事务受控串行或 BEGIN IMMEDIATE，busy_timeout、有界重试；版本条件更新检查 rowcount，批量冲突整批回滚。提交后才发 ChangeSet。

WAL 适合本地并发读，数据库不放网络共享盘或热文件同步目录；明确 synchronous 策略，重要写入优先 FULL。备份使用 Connection.backup 或验证的一致性快照，不能只复制 .db 丢失 WAL。多库备份暂停写入并记录一致性边界，不能声称两个独立 backup 是同一事务快照。

当前 .dcalbackup format1 只有 manifest + calendar_data.json，**不含 AI 会话或凭据**。新格式需明确新成员和 format version，继续读取旧格式。Chat 历史导出需要用户同意和可移植加密设计；DPAPI 密文不能简单搬到另一设备解密，凭据默认不备份。

恢复先隔离验证 integrity_check、foreign_key_check、业务校验，停止写入并关闭连接后切换；保留旧数据库及WAL组，不把新.db与旧WAL混用。刷新失败与业务事务失败分清，保留可恢复快照。

## 安全迁移顺序

1. 阻止旧程序并发写入，检查空间/权限/Schema；保存原始 JSON 独立副本与 SHA256，不覆盖原备份。
2. 严格 validate_data，不使用 Store.load 跳过坏记录后的数量当迁移成功；异常停止并报告，不擅自修复记录。
3. 在隔离临时数据库一个事务导入，记录源hash、计数及迁移版本。重复迁移识别源，避免重复实体。
4. 对比 ID、所有字段、顺序、分类有效色、历史规则/打卡/todo、日期状态及提醒设置；跨周、节假日、请假、跨年做 JSON/SQLite 行为对照。
5. 新备份恢复和独立更新器识别到位后再切换权威存储标记；只保留一个写入源，不长期双写。
6. 残留旧 JSON 仍可能被旧 EXE 打开。必须设计旧版可安全拒绝的升级门禁及进程协调，原件放非活动备份位置；不能伪造低Schema。逐个支持的旧版验证拒绝且原数据字节不变。
7. 切换失败回滚 marker/原文件/内存。新库已接受用户新写入后不能静默回退旧 JSON；需兼容新版恢复或经验证导出，不能丢弃新数据。
8. 正式 EXE 隔离演练更新→迁移→重启→备份→恢复→失败回滚后才允许真实迁移。

App Version、Data Schema、SQLite Migration、Backup Format、Chat Journal、API、未来Sync Protocol独立管理；本轮均不修改。
