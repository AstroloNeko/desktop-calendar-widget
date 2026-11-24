# 微信 B2 探针验收

## 2026-11-24 验收结果

- Windows 真实网络环境成功连接 Cloud Run，并持续轮询空队列。
- 成功领取真实微信测试消息。首次完成回执返回 `notify_status=sent`，重复提交返回 `already_completed=true`。微信端验收已确认。
- 未标记消息拦截生效。此前无法定位的 processing 任务已记录为已知遗留。
- 10 项自动化测试全部通过。断网恢复和回执超时重传仅通过模拟验证。
- 保留 `--claim-audit`。探针收到并解析 HTTP 响应、识别出任务对象后，检查消息标记之前，使用 Windows 本机 UTC 时钟写入 `observed_at_utc`。云端领取时间以响应中的 `claimed_at` 为准。

## 运行方法

在 Windows 上从仓库根目录运行。探针使用 Python 标准库，所有凭据只从当前进程环境读取。准备 `CALENDAR_SERVER_URL`（HTTPS 根地址）和 `CALENDAR_WORKER_TOKEN`（Worker 凭据）。若从微信回调地址取值，只保留协议、主机和端口。

1. 运行 `python tools/wechat_b2_probe.py` 做公共健康检查。此时 Worker 身份仍待验证。
2. 在 Firestore 的 `wechat_jobs` 检查 pending 队列，确认没有真实业务消息，并停止其他领取任务的 Worker。云端先领取再返回正文，Windows 只能在领取后检查标记。
3. 运行 `python -u tools/wechat_b2_probe.py --isolated-test-queue`。探针先用随机错误 Token 请求 claim，预期收到 401 或 403，再用配置的 Token 每 15 秒领取一次。观察至少两次空队列。
4. 做断网恢复验收时，在空队列期间临时断开网络，等待一次失败后恢复连接。观察健康检查恢复，以及后续一次成功的空队列轮询。
5. 在微信测试号发送 `B2_TEST 回执验收`。独立标记后使用空格或换行。探针处理一条测试消息后，将同一模拟回执再提交一次，检查 `already_completed` 与 `notify_status`，然后退出。在微信核对只收到一条“B2_TEST 模拟回执，未修改日历。”。
6. 随时可用 Ctrl+C 退出。探针日志使用 UTC 时间，可与 Firestore 的 `claimed_at` 对照。

需要按文档 ID 核对时，在领取命令中加上 `--claim-audit FILE`，使用本机未跟踪目录中的新文件，例如 `build/wechat-b2-claim.jsonl`。每条记录包含服务器根地址、`job_id`、`msg_id`、服务端 `claimed_at` 和本机观察时间。请在本机保管这份记录。

**领取异常。** claim 超时可能已经产生 processing 记录。探针等待 15 秒，使用公共健康检查确认连通性，再发起新一轮领取。若领到未标记消息，探针立即退出。按 UTC 时间在 Firestore 检查 processing 记录，保留原记录供人工核对。

**提交异常。** complete 遇到超时、连接中断、HTTP 408、429 或 5xx 时，探针在进程内保留原 MsgId 和请求体，间隔 15 秒重传。404、409 和其他终止错误需要人工核对。探针没有跨进程台账，中断后应先检查云端记录。完整的本地恢复台账属于 B3。

**模拟验收。** 运行 `python -m unittest discover -s tests -p test_wechat_b2_probe.py -v`。回环服务可以在保存回执后断开连接，验证下一次请求仍是逐字节相同的回执。实际微信送达次数需由微信接收端核对。
