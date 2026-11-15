# Stage B2 — isolated SQLite Calendar Repository

基线 B1 `1268a3f70950f0d3bafb52ef70f9762b93bb04a9`，1056 项测试。
分支 `feature/sqlite-calendar-repository`。本阶段没有迁移真实数据，也没有
切换桌面默认存储、修改 JSON Schema 9、版本号、备份格式或 ChatJournal。

## 实际契约核查及扩展

B1 prepared changes 能表达受影响集合的最终候选（含删除），但 JSON 的
先换内存/Store.save 不能直接当 SQLite 提交。SQLite 使用独立事务实现。
共用部分仅为 detached reads 和结构校验；JsonCalendarRepository 提取
`_prepare_changes`，原 JSON 原子替换/摘要故障判断不变。

RepositoryCommit 增加可选 operation_id/replayed；Contract 增加 close。
SQLite 特有 apply_changes(operation_id=...) 与 receipt(id) 支持持久回执，
JSON 没有伪造这项能力。默认 A2/A3 无需后端类型判断。

现有 Store.load 仍需要 read_document 形状；SQLite 从关系表重建临时文档 DTO
供原模型读取，不落 JSON 文件。这是读取兼容桥，不是整个数据库的单列存储。

## Schema 1（独立于 JSON Schema 9）

`sqlite_calendar_schema.py` 是显式建表定义，PRAGMA user_version=1，独立
application_id 标识。初次建表、Calendar 元数据和版本标记在一个事务中完成。
不支持旧 SQLite 升级或未来 Schema 降级；损坏/其它应用/非空未识别库拒绝。
只有一个默认 Calendar，ID 为一次生成并持久化的 UUID，所有业务表带 calendar_id。

| 表 | 内容/约束 |
|---|---|
| schema_migrations | 初始版本及创建时间 |
| calendars | 稳定 ID、名称、timezone=local-naive、单调 revision |
| events | 原模型所有标量字段、order_key、revision；类型/时长/颜色来源 CHECK |
| categories | 原 ID/name/color/sort_order/created_at、order_key、revision |
| routines | 原标量字段、order_key、revision；kind/frequency/interval CHECK |
| routine_weekdays | routine_id/weekday 唯一，0..6，顺序 |
| habit_completions | routine_id/occurrence_date 唯一，保留原打卡顺序 |
| habit_schedule_history | routine_id/until_date 唯一，validated rule JSON、顺序 |
| date_states | calendar_id/day 唯一；leave/holiday，normal 删除覆盖 |
| preferences | 每键 JSON 值，device/reminder/unclassified scope |
| reminder_delivery_state | notified plan_key 唯一，设备投递辅助状态 |
| operation_receipts | operation_id 唯一、请求 hash、提交 revision、类型/受影响 ID 摘要 |

Event/Routine/Category ID 是不透明非空字符串，不强制旧 ID 为 UUID。复合外键
确保 Event 分类属于同 Calendar；分类删除外键延迟到提交验证，必须在同事务
完成 A3 有效色冻结与解绑。Habit 子表随删除 cascade。索引覆盖 due/category/done
以及 routine enabled。SQL 标识符来自固定 Schema，不接受模型生成的 SQL。

偏离原草案：不新增 start_date/local_time 两份权威时间，暂保留原 due ISO 文本
和 has_time，避免改变日期/DST/时区语义。23:59+has_time=False 原样保留，不变成
定时任务。DDL、skip-day、调休、请假继续调用 Store 规则；范围查询仍是现有
投影算法，不使用可能漏掉跨月/skip-day 的简单 SQL 起始日期过滤。
旧 created_at 仍保留原始本地时间，不伪造 UTC。新 Calendar timezone 标记
local-naive 是明确的兼容策略，不冒充已完成 IANA/DST 迁移。

## 状态来源和事务顺序

Store 是唯一运行时对象集合。Repository 不维护第二套可变业务缓存。
读取返回 Store 在 loaded revision 的独立快照；不会偷偷刷新其它实例写入。
显式 Store.load 才重新取得一个一致的数据库快照；Headless 保持单业务线程。

1. 原 A1/A2/A3 构造候选，预览零写入。
2. BEGIN IMMEDIATE 获取写锁。
3. 在事务内比对数据库 ID/revision、expected_revision 和 Store loaded revision。
4. 只更新变化记录/顺序/子记录，删除确实被移除的 ID；不是清空重建全库。
5. 同事务更新 Calendar revision、写入操作回执。
6. COMMIT 成功后才发布候选到 Store，并更新 loaded revision。
7. 原命令系统记录 Undo；上层再刷新 UI/提醒。

对相同候选回滚只读事务，不 COMMIT、不推进 revision、不写 no-op 回执。
一批 Event 仍一次 COMMIT/一次 Undo。Habit 规则历史和打卡沿用 A3；原本不可撤销
的打卡/删除不借此改变。事务期间没有网络请求。

每次数据库工作打开并关闭一个连接，启用 foreign_keys、WAL、synchronous=FULL，
busy timeout 默认250ms。连接不跨线程；关闭 Runtime 标记仓储关闭且没有遗留连接。
Runtime 的关闭重试保留 A4 有界停止语义，慢模型超时后可再次完成关闭。

## Revision、冲突、故障

SnapshotToken.session 在 SQLite 中是稳定 Calendar ID，generation 是持久 revision。
重新打开后不重置。另一个合法写入使旧 Store 的提交失败；即使手工传入最新 token，
也不能跳过旧 Store 必须 reload 的检查。文件锁并不替代这一检查。
同一 Store 不允许再挂第二个 SQLite adapter。

事务内失败且连接仍 in_transaction：ROLLBACK，Store 未发布，历史不入栈。
锁忙/SQL 存储异常通过已有服务映射 storage_unavailable；约束错误为 validation，
Revision 为 operation_conflict，未来 Schema 保持 incompatible_data。
回滚无法确认或 commit 异常且不能核验回执：MutationUncertainError/load_error
阻止继续写入，不自动重放。commit 后响应异常若能核验对应持久回执，则按真实成功
发布状态，不报告假失败。没有照搬 JSON 文件摘要机制。

## 回执能力与限制

每次实际提交生成 operation_id；显式传入相同 ID + 完全相同候选可跨重启读取
既有回执，不再次写入。相同 ID 换参数拒绝；不同 ID 代表不同用户操作，不按标题
或日期去重。重试回执后 reload 当前真实状态，不能把旧候选重新覆盖较新提交。
无变化提交不占用 operation_id；调用者不能把它当作已保存的幂等回执。

内部一次性请求 hash 使用变更摘要及基线 revision，避免每次序列化全部对象；
显式可重试请求 hash 仍覆盖完整候选。operation_type 当前是 prepared 字段集合，
不是新增 Agent 命令协议。回执尚无归档策略。

这是 **SQLite 业务层** 能力。现有 A2/Chat 请求未将 ChatJournal operation_id
贯通到仓储；不宣称两个库 exactly-once。B3/C 需设计 ID 贯通和结果查询，不能
把一次新建重试重新随机生成对象 ID 后冒充相同请求。

## Legacy Bridge 分类

| 分类 | 路径及实际处理 |
|---|---|
| A 已适配 | A2 Event/批量/拖拽命令、A3 Habit/Category/Date State prepared commits，混合 Undo；读快照 |
| B 最小后端接口 | read_document 关系表 DTO、close、SQLite receipt/backup_to；Runtime 显式 factory |
| C JSON-only | 正式 .dcalbackup、恢复 staging/自动迁移备份；SQLite restore_store 明确拒绝 |
| D B3 前必须完成 | 默认后端切换、旧 JSON 导入、设备设置拆分、备份格式及更新器协调、旧 EXE 门禁 |

SQLite Store.save 只允许设备 preferences 和 notified 辅助状态变化；先检查业务
集合/日期是否与当前持久快照一致，再以相同 revision 执行辅助事务。直接改 Event
后 save 明确失败，不把陈旧内存整库覆盖。Store 的旧 direct upsert/delete 不能作为
SQLite 正式入口，必须使用 Application Service。

preferences 未知键以 unclassified 原样保留，不默认列为未来同步事实。geometry/
theme/hotkey 属设备；native_reminder_state 等属提醒策略/账本兼容状态。notified
独立投递表。本阶段没有自动通知后台线程，也未证明正式 Windows 提醒全链已迁库。

## 显式运行及备份

```python
from calendar_runtime import HeadlessRuntime
from sqlite_calendar_repository import SqliteCalendarRepository
with HeadlessRuntime(explicit_temporary_path / 'calendar.sqlite3',
                     repository_factory=SqliteCalendarRepository) as runtime:
    runtime.events.create_quick('Synthetic', explicit_date)
```

没有 GUI 默认开关；拒绝 .json 路径，不自动导入真实 JSON。测试提供隔离配置、
ChatJournal、Mock LLM，HTTP 使用原认证与路由。应用服务及 HTTP 不写原始 SQL。

backup_to(new_path) 独占创建目标，用 Connection.backup 取得包含 WAL 的一致快照，
验证 Schema/quick_check/FK；目标已存在则拒绝。失败清理仅本次创建目标，不改源。
测试恢复方式是把备份作为新的隔离数据库打开，比对业务和 revision。没有实现热替换
运行中的正式数据库，也未修改正式备份容器。

## 测试与 Windows 回归

- 原基线1056；新增54：16个双后端共享契约、11个实际 SQLite Runtime/Chat/HTTP
  测试、27个 SQLite 特有事务/故障/并发/备份测试。完整 **1110/1110** 通过。
- Python compile：146个文件通过；git diff --check通过。
- 真实 A2/A3 Event CRUD/批量/Undo、Habit 周期/打卡历史、Category 冻结、日期
  状态、skip-day 末日 DDL、预览零写与重启验证；不是仅测原始 SQL。
- 两个 Store/连接的过期写入、busy lock、安全拒绝、单行 SQL trace、commit前后
  故障、无法核验结果、重复 operation_id、初始化事务回滚、损坏/未来 Schema。
- JSON 默认真实 Tk：四主题 Compact/Global 事务/Modal 8组；Flow/Timeline
  move/resize/cancel/Undo/搜索通过；Chat GUI 8组 Mock+loopback HTTP，errors=[]。
  实际192 DPI（200%）。脚本操作真实窗口，不冒充人工全视觉验收。
- 未调用真实 LLM、未构建正式 EXE、未迁移真实用户数据、未执行 B3。
- 已有 HTTPError 资源清理及恶意重复 ZIP fixture 警告仍会输出，不是测试失败。

## 性能证据

`python tools/check_repository_backends.py`，Python3.14.7 Windows x64，临时本地
目录，每指标3次中位数；每组另有20个Habit及打卡、10个Category和日期覆盖。
业务测量走服务与查询投影，不只跑原始 SQL。fixture 装载时间不计入修改耗时。

| Event数/后端 | 加载ms | 日期查询ms | 单项修改ms | 批量50ms | Undo+Redo ms | 日期提交ms | 辅助保存ms | 文件bytes |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 JSON | 0.765 | 0.022 | — | — | — | 10.238 | 8.719 | 14140 |
| 0 SQLite | 4.822 | 0.016 | — | — | — | 4.731 | 7.013 | 118784 |
| 1000 JSON | 10.499 | 13.395 | 23.524 | 51.487 | 67.902 | 23.053 | 22.075 | 490921 |
| 1000 SQLite | 29.166 | 13.536 | 7.687 | 35.613 | 35.871 | 5.077 | 17.637 | 434176 |
| 10000 JSON | 108.756 | 159.662 | 117.247 | 298.931 | 347.547 | 109.194 | 104.362 | 4801921 |
| 10000 SQLite | 255.419 | 145.134 | 31.075 | 221.861 | 178.008 | 5.836 | 110.192 | 3235840 |

关闭约0.001–0.002ms，因为工作连接均在操作返回前已关闭。上述0 Event组并非零
辅助数据。SQLite加载更慢来自表读取/模型严格往返校验及完整性检查；不隐藏这一
取舍。当前仍加载完整Store，范围查询仍 O(N)，还不是懒加载查询内核。
首次实现曾对每次修改做全行比对，已优化为事务确认 loaded revision 后按候选差异
更新；10k单项从约316ms降到31ms。测试机器调度有波动，不作为性能 SLA。

## B3 前置清单（本轮未实施）

1. 严格 JSON9字段映射；不能用宽容Store跳过坏记录后的数量冒充迁移完整。
2. 保留非UUID ID，重复/空/损坏ID报告并停止，不按标题合并或自动重编号。
3. Habit until排他边界、todo、原打卡顺序、规则历史逐字段对照。
4. 确认旧时间来源时区；保留 naive due / has_time / 23:59 占位及 DDL实际末日。
5. 未知设置、设备几何/主题/热键、Reminder Ledger 拆分和同步范围明确。
6. 迁移 Calendar ID和初始 revision 一次建立，备份还原使待执行预览/Undo失效。
7. 新备份格式版本、旧 .dcalbackup 导入、SQLite官方备份与关闭连接后的安全恢复。
8. 防止旧EXE读旧目录或写旧JSON；更新器安装/回滚/多实例协调和权威存储标记。
9. WAL/SHM必须按数据库生命周期处理，禁止复制活跃主db冒充完整快照。
10. 迁移前独立原件/哈希、失败回滚、已产生新写入后不能静默回退旧JSON。
11. 明确操作回执保留/归档、服务请求ID贯通，不能宣称跨ChatJournal原子事务。
12. 真机正式EXE隔离升级→迁移→备份→恢复→失败恢复，之后才允许默认切换。

B2到此停止。SQLite只用于显式隔离运行，默认JSON和正式用户数据均未改变。
