# Calendar API v1 契约草案

本文件区分已实现 Chat API 与未来 Business API；不修改现有路由，不安装 FastAPI，不开放公网。数据库列不能直接成为API字段。

## 现有 Chat API

`calendar_chat_http.LocalChatHTTP` 默认127.0.0.1:18746，Bearer认证、Host校验、拒绝浏览器跨站、并发/速率/大小限制。health无需Bearer但仍受Host检查。owner由适配器指定local-owner，不接受客户端身份。

| 方法/路径 | 现有含义 |
|---|---|
| GET /v1/health | status/api_version |
| POST /v1/sessions，{} | 新会话 |
| POST /v1/chat/messages | session_id/message/request_id |
| GET /v1/sessions/{id} 或 /messages | 历史 |
| GET /v1/sessions/{id}/pending | 待处理操作 |
| DELETE /v1/sessions/{id} | 清会话，保留安全回执 |
| GET /v1/operations/{id}?session_id=… | 查询结果 |
| POST /v1/operations/{id}/confirm | session_id + revision |
| POST /v1/operations/{id}/cancel | session_id |

Python Service 有 list_sessions，但没有 GET /v1/sessions 列表HTTP路由。现有 envelope/status、clarification、pending_operation、operation_result、错误结构继续兼容。当前明确单对象创建/修改可自动执行；删除确认、歧义澄清与写入白名单仍保留。多工具不意味着批量授权。

## 未来 Business API（尚未实现）

| 方法/路径 | 语义 |
|---|---|
| GET /v1/calendars | 认证主体可访问日历；首期一个个人日历 |
| GET/POST /v1/events | 查询/创建逻辑事项 |
| GET/PATCH/DELETE /v1/events/{id} | 读取/局部修改/删除 |
| GET/POST /v1/habits | 查询/创建习惯，不将legacy todo静默转换 |
| GET/PATCH /v1/habits/{id} | 详情/局部修改 |
| POST /v1/habits/{id}/completions | date + completed，显式设定而非toggle |
| GET/POST /v1/categories | 查询/创建 |
| PATCH/DELETE /v1/categories/{id} | 修改；删除冻结关联有效色，不删事项 |
| GET /v1/deadlines | 真实末日唯一条目，复用complete_ddl_groups |
| POST /v1/event-previews | 零写入排期预览；提交仍重新校验版本 |

日期状态、批量外部写入等到事务统一后再设计，不因API存在而开放AI批量/Habit删除。

查询条件：calendar_id、from/to日期闭区间、category_ids多选、include_uncategorized、done、event_type。Event范围查询按实际执行日期与范围相交，不仅筛开始日期；DDL按真实末日。分页limit默认50/最大200，稳定(start_date,id)排序，不透明cursor绑定过滤与快照revision；数据变动需要重分页提示。跨度上限通过现有365有效天规则性能测试后确定。

## DTO、默认值与 Patch 三态

创建示例（建议契约）：

```json
{"calendar_id":"personal-id","title":"整理记录","start_date":"2026-11-20",
 "local_time":null,"event_type":"urgent","duration_days":3,
 "skip_non_working_days":true,"end_as_ddl":true,"category_id":null}
```

ID服务生成，旧ID作为不透明字符串接受。类型保留general/urgent/ddl；不是Category。start_date必填，local_time默认null；创建未指定颜色，有分类继承、无分类使用当前默认色。默认规则由共享业务验证器确定，不由LLM补齐。纯日期reminder_minutes默认为null，原生提醒偏好另属策略，不编造具体时间。

```http
PATCH /v1/events/opaque-existing-id
If-Match: "7"
Idempotency-Key: illustrative-request-1
Content-Type: application/json

{"title":"动物实验"}
```

未传=保留；null只对nullable字段有效；显式值经严格校验。title:null、duration:null、未知字段拒绝。category_id:null清分类时冻结有效色并转override；inherit需有效分类；color-only改为override；inherit与独立color同时提供应拒绝矛盾，不猜测。修改分类未指定颜色模式时保留原override/继承策略，“恢复跟随”显式传inherit。

响应只返回筛选后的canonical对象和派生effective_color/effective_dates/deadline_date；派生字段只读。禁止客户端覆盖habit_done、schedule_history、notified、owner_id、created_at。

Habit示例：`{"frequency":"weekly","interval":1,"weekdays":[1,3],"color":"#52B788"}`。一个原子Patch，保留其它字段。weekday0=周一；变更默认日历今天生效，响应effective_on；历史until排他边界只读。缺失月日跳过，周间隔锚点沿用现有规则。提醒开启必须有合法时间，关闭不删除习惯。

Completion：`{"date":"2026-11-20","completed":true}`。重复true不翻转；只允许计划日新增，允许取消已有历史完成；统计用rule_on。周期变更与check-in分别进行冲突处理。

## 响应、冲突和幂等

Business新响应建议（不替换旧Chat envelope）：

```json
{"data":{"id":"opaque-existing-id","title":"动物实验","revision":8},
 "meta":{"request_id":"r1","operation_id":"op1","changed":true}}
```

```json
{"error":{"code":"revision_conflict","message":"事项已变化，请刷新后重试。",
 "fields":[],"retryable":false},"request_id":"r1"}
```

建议状态：400 malformed_request；401 unauthenticated；403 forbidden；404 not_found；409 revision_conflict/idempotency_conflict；422 validation_failed；428 precondition_required；429 rate_limited；503 storage_unavailable。云端私人对象可统一404防止存在性泄露。schema_too_new准确提示兼容程序，不要求重建数据。

所有可重试写入使用幂等键，范围为可信主体+操作/资源；同key同canonical请求返回原结果，异payload409。回执与业务数据同事务。不能把现有Chat request_id机制宣称为通用CRUD事务幂等已经完成。明确回执保留期，过期不静默冒险重放。

PATCH/DELETE必须If-Match；权限先于对象版本检查。排期预览还校验日历规则revision；批量任一冲突全拒绝。无变化changed=false、不额外增加Undo。持久化成功但网络或UI刷新失败，重试同key/查询operation得到真实结果。

## 本机与未来云端

本机继续127.0.0.1、当前用户Token、桌面生命周期；不得改0.0.0.0或扩大CORS。Cloud另行HTTPS、用户认证、CalendarMembership隔离、设备与凭据管理。不能复用本机固定Token当多用户登录，不信任客户端user_id/owner_id或AI判断权限。

API版本独立于表结构；新增可选字段可兼容，语义或默认值变化需明确兼容适配/新版本。实现前将草案固化OpenAPI/JSON Schema和fixtures，再生成Dart客户端；FastAPI只做Adapter，不能跳过Application Service直接SQL。
