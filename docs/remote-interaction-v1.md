# 远程结构化交互 v1 草案

本文是客户端 Mock 联调契约。Cloud Run 生产版本无需修改即可继续使用纯文本路径。字段命名与上线开关由客户端和 Antigravity 共同确认。配套 [JSON Schema](remote-interaction-v1.schema.json) 包含完整回执和输入约束。

## 已部署字段与客户端兼容

- `/api/pairing/activate` 与 `/api/notifications/session` 的可选 `display_name` 只用于显示。客户端仍以经核验的服务器、内部 `user_id` 和凭据判断授权。激活时可保存昵称，通知会话提供当前昵称。
- claim 的 `channel` 为 `qq` 或 `wechat`。QQ 的 `conversation_key` 为 `qq:<channel_app_id>:<QQ OpenID>`，与 v0.13.0 收到的兼容 `openid` 完全相同。微信 `conversation_key` 沿用原 OpenID。两字段同时出现时须一致。旧服务只有 `openid` 时沿用原映射。
- 会话仍用 `wechat-verified- + fingerprint([server_account_scope, canonical_key])`。台账及 request ID 仍按服务器、账号和原始 MsgId 查找。无需重写历史数据；缺少渠道元数据的旧记录显示“未知渠道”。已封存的 complete 字节照旧重传。
- `created_at_ts` 是发送时的 Unix 秒整数，按原 UTC+8 日历锚点处理。旧响应继续使用 `message_time_local` 或 `wechat_create_time`。提供了 `owner_id` 时必须与已核验内部账号相同。

## 能力协商及请求示例

成功的 `POST /api/notifications/session` 响应可在现有字段之外公布 `"interaction_schema_version": 1`。缺少该值或为未知版本时，客户端只发旧回执，按钮和引用输入也不能当成文字执行。原生渠道字段与能力协商相互独立。

旧 complete 的 `ok`、`error`、`operation_id` 和 `reply_text` 保持原样。协商成功、账号与渠道路由完整时，`POST /api/jobs/<msg_id>/complete` 可带以下附加业务结果。

```json
{
  "ok": true,
  "error": null,
  "operation_id": null,
  "reply_text": "有多个匹配事项，请选择要操作的一项。\n1. 2026-11-28 · 样例组会 · 10:00",
  "business_receipt": {
    "schema_version": 1,
    "result_type": "candidates",
    "summary": "当时等待补充信息",
    "events": [
      {"index": 1, "title": "样例组会", "date": "2026-11-28", "end": "2026-11-28", "time": "10:00", "done": false}
    ],
    "available_actions": [
      {"id": "q1eWrv5ku2F-Ah9dQWtUd-jHhYVfWs32", "kind": "select_candidate", "index": 1, "expires_at_ts": 1795837500}
    ],
    "reference_until": 1795837500
  }
}
```

服务器继续返回原 complete 确认。重复 complete 不重新渲染或重复发通知时，也应保持相同执行证据。

```json
{"status":"done","exec_ok":true,"operation_id":null,"notify_status":"sent","already_completed":false}
```

已完成写入可带 `outcome`，其 `committed`、`goal_satisfied`、`changed` 可为 `true`、`false` 或 `null`。例如 `committed:true, goal_satisfied:false` 是 `partial`，表示已提交但效果未完整核验，不能渲染成成功。确认卡带 `pending_confirmation.stage`；高风险删除的 `reviewing_batch` 和 `awaiting_final_confirmation` 各有新的动作。空数组表示没有可授权动作。

按钮与引用仍走原 claim 队列，不增加客户端 API。

```json
{
  "job": {
    "msg_id": "qq_synthetic_action_01",
    "owner_id": "sample-internal-account",
    "channel": "qq",
    "channel_app_id": "123456789",
    "conversation_key": "qq:123456789:sample-sender",
    "created_at_ts": 1795837000,
    "interaction": {
      "schema_version": 1,
      "kind": "action",
      "source_msg_id": "qq_synthetic_query_01",
      "action_id": "q1eWrv5ku2F-Ah9dQWtUd-jHhYVfWs32"
    }
  }
}
```

引用原查询的第二项时，claim 保留用户文本 `把第二个改到下午三点`，把上例 `interaction` 换成下例。可选 `index` 为一基编号；若文本也含编号，两者须一致。引用可定位查询或候选中的单项修改、完成状态，随后走待确认预览。引用刚完成的写入并回复「撤销」，等同于点击该记录的撤销按钮。

```json
{"schema_version":1,"kind":"reference","source_msg_id":"qq_synthetic_query_01","index":2}
```

## 本机授权与降级

- 只能在与来源匹配的当前服务器、内部账号、渠道、应用、会话中使用本机记录的动作。原消息必须已成功提交给服务器。Event ID、当前快照指纹和 Pending Revision 留在本机，云端只回传动作 ID。
- 确认和取消检查待确认计划、当前可见预览、阶段和期限。候选按钮检查原候选集合和事项 Revision。高风险删除的两个阶段继续独立确认。
- 动作在执行前在原账号台账登记。重复点击，即使云端换了 MsgId，也不能再执行。若主机在登记后、记录结果前关闭，沿用待核查策略。撤销只允许当前内存历史栈顶的那一笔已核验写入，关闭程序或栈顶改变后，旧撤销按钮会拒绝执行。直接发送「撤销」或「撤销上一步」只检查本会话最近一条远程结果，不越过后续消息寻找旧写入。
- 结构化回执由本机结果生成。模板失败、版本不识别或渠道不支持时可直接发送 `reply_text`。客户端不生成 QQ Markdown、按钮布局或平台消息 ID。

## Antigravity 对接清单

1. 在现有通知会话响应加入按部署开关控制的能力字段。灰度关闭能力时，也应继续接收尚未上传的旧业务回执及原纯文本字段。
2. 扩展 complete 的可选字段解析与幂等保存，将 `business_receipt` 和原任务、原发送者、实际发出消息 ID 绑定。结果未知、部分完成、待确认须分别呈现。纯文本兜底使用已保存的 `reply_text`。
3. 校验 QQ 按钮事件，验证机器人、调用人、内部账号、原对话、已实际发出的动作 ID 和有效期。通过后向原 claim 队列写入结构化 `interaction`，使用稳定的点击 MsgId，继续保留原有等待提醒策略。
4. 校验引用对象是该机器人向同一账号及会话发出的已提交消息。把平台引用 ID 映射回 `source_msg_id`；无法验证时回复重新查询，避免把引用上下文或用户填写的 Event ID 当成授权。
5. QQ Markdown、原生按钮和快捷指令由云端呈现，标题等用户文本须按平台语法转义。待确认的高级删除卡必须使用当前阶段新动作。微信历史及旧 complete 响应语义继续保留。
