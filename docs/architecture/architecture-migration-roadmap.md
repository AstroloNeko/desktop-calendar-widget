# 渐进迁移路线与验收

基线 `f7e622c`，分支 `feature/calendar-chat-service-phase1`，本轮仅新增这五份文档，未改代码/版本/Schema，未迁移用户数据，未提交或推送。

文档索引：

- [当前架构审计](current-architecture-audit.md)
- [目标业务分层](target-domain-architecture.md)
- [SQLite模型与迁移](sqlite-data-model-plan.md)
- [API v1契约草案](calendar-api-contract-v1.md)

## 建议顺序

不要直接从SQLite建表开始。先统一输入与事务边界，在JSON上验证Application接口，再替换Repository；HTTP对外暴露最后进行。即 A → C的内部服务部分 → B → C的HTTP部分 → D，而非同时改全部层。

| 小阶段/建议分支 | 具体模块/提交范围 | 风险和验收标准 |
|---|---|---|
| A1 `refactor/event-input-validation` | app.EventEditor.save/parse_event_due、Gateway._patch提取共享candidate验证；原import保留兼容 | 同显式输入GUI/AI同结果；标题Patch不污染；无时间/DDL旧边界不变；不动持久化 |
| A2 `refactor/calendar-operation-services` | EventCommandHistory外增加薄Application入口，复用preview/commit/bulk，返回类型化结果 | 拖动零写、释放一次save/Undo；失败回滚；不改窗口机制 |
| A3 `refactor/routine-category-transactions` | Routine删除/打卡、分类联动、日期状态范围写入统一事务 | 历史保留、类别删除冻结色、范围一次save；提醒失效；故障注入验证 |
| A4 `refactor/headless-calendar-composition` | 拆TkDispatcher/ChatRuntime组装；core主题设置移到设备适配 | 无Tk服务可测试；桌面队列/Modal/退出不回退；SecretStore保留Windows实现 |
| B1 `refactor/json-repository-boundary` | JSON Repository包现有Store，查询返回脱离快照；拆设备偏好 | 现有文件字节语义/旧Schema兼容；所有写入有明确入口，无双写 |
| B2 `feature/sqlite-repository` | 隔离SQLite adapter、单位事务、版本/回执、索引 | 同一Repository契约跑JSON/SQLite；并发冲突、断电/锁忙/磁盘失败不部分提交；不切用户数据 |
| B3 `feature/sqlite-migration-backup` | data_compat/data_backup/backup_ui/update协调的最小迁移适配 | 严格导入对照、正式EXE迁移/恢复/旧版拒绝；新备份读旧容器；保留原件 |
| C `feature/calendar-business-api-v1` | DTO/OpenAPI、版本化Business HTTP adapter；Chat改调相同服务 | 缺失/null/值契约、身份/权限、幂等、分页、冲突；旧Chat路由不变；仍loopback |
| D `spike/multiplatform-adapters` | 隔离FastAPI组装/Flutter typed-client验证，未来独立任务 | 无需实现登录/云同步；服务测试不需要Tk；不能把本机Token作为云身份 |

以上分支是建议，**本轮没有创建**。每阶段可独立回退代码；一旦数据已迁移并产生新写入，不能用旧二进制回滚数据。每个PR都必须写明数据格式是否变化与支持的旧读者范围。

## 分阶段测试门禁

1. A：现有Event/批量/拖拽/Habit/Chat测试；同输入双入口对照、Patch属性保留、失败回滚、幂等重试、历史与Undo、一次操作保存次数。UI四主题、Compact/Global、Modal及200%DPI。
2. B1：Repository契约，旧Schema 1..9代表fixtures、损坏/重复ID拒绝、未知设置保留；JSON与原Store读写结果一致。
3. B2：FK/约束、事务冲突/回滚、两个连接、锁忙、连接线程所有权、WAL未checkpoint备份、恢复失败、操作回执与业务同事务；SQL查询不能漏跨月skip-day事项。
4. B3：隔离副本字段和ID逐项对照、实际日期/DDL/有效色/历史统计、重复迁移、原件hash不变、旧EXE拒绝、两个安装目录、升级中断、更新器重启路径、备份恢复后的重启。真实数据只读调查，不用于破坏性实验。
5. C：HTTP认证、字段allowlist、越权calendar_id、跨会话隔离、稳定分页、过期预览、时间/时区、重复请求、并发批量、禁止直接改Store；桌面/Chat/API相同服务结果。
6. D：无头Linux/Windows测试，IANA DST边界、纯日期不跨天、客户端契约兼容。移动端后台通知和系统密钥链须将来真机验收，不能靠Windows smoke代替。

## 当前测试证据与限制

已有42个test模块覆盖核心规则、命令、Habit、批量、Chat、备份/更新、提醒、窗口/DPI等。上一次实现的记录为915/915；本轮首次重新执行915项时出现42个errors，已看到备份 `os.link` 被沙箱以 WinError5拒绝的调用栈。不能把该次运行记作通过，也不能未经逐项确认将所有errors归因同一原因。

正常权限重跑申请上轮因自动审批服务用量限制未执行；不是代码测试失败的进一步证据。本轮仅文档，不据此修改备份实现。本轮最终检查结果在交付报告说明；未重新做GUI或真实模型调用。

## 待决策项与明确暂缓项

- 开始实际SQLite迁移前确认旧时间的来源时区、共享Habit按人还是共同完成、备份是否需要用户选择导出聊天历史。
- 当前数据Schema9高于某些已发布二进制能力；不能用旧EXE打开开发数据目录做迁移实验。
- 暂缓用户注册、共享邀请、云端部署、WebSocket/CRDT、完整outbox/tombstone、Flutter产品UI与AstrBot接入。
- 最先可批准的代码任务是A1，不是全量重构app.py或直接把JSON转数据库。
