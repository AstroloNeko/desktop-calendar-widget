"""Standalone WeChat B2 probe. Run with --help for live acceptance options."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desktop_calendar.remote_transport import (
    CLAIM_PATH, INTERVAL, Client, NoRedirect, StatusError, Unavailable, transient,
)


RECEIPT = json.dumps({
    "ok": True, "reply_text": "B2_TEST 模拟回执，未修改日历。",
    "operation_id": None, "error": None,
}, ensure_ascii=False).encode("utf-8")


class ProbeError(Exception):
    """Terminal diagnostic safe to show without disclosing request data."""


def log(message):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"{now}  {message}", flush=True)


def check_health(client):
    if client.request("/").get("status") != "ok":
        raise ProbeError("健康检查响应与契约不符。")
    log("公共健康检查通过。")


def reject_invalid_token(client):
    try:
        client.request(CLAIM_PATH, secrets.token_urlsafe(32))
    except StatusError as exc:
        if exc.status in (401, 403):
            log(f"错误 Token 被拒绝（HTTP {exc.status}）。")
            return
        raise
    except Unavailable:
        raise ProbeError("错误 Token 检查未收到有效响应，请按时间核查 processing。") from None
    raise ProbeError("服务端接受了随机错误 Token，请核查鉴权与 processing。")


def wait_for_job(client, token, claim_audit=None):
    check_recovery = False
    while True:
        if check_recovery:
            try:
                check_health(client)
            except (StatusError, Unavailable):
                log("公共健康检查尚未恢复，15 秒后再检查。")
                time.sleep(INTERVAL)
                continue
            check_recovery = False
        try:
            result = client.request(CLAIM_PATH, token)
        except (StatusError, Unavailable) as exc:
            if isinstance(exc, StatusError) and not transient(exc.status):
                raise
            log("claim 结果不确定，请按 UTC 时间核查 processing。15 秒后先检查公共健康接口。")
            check_recovery = True
        else:
            if "job" not in result:
                raise ProbeError("claim 缺少 job，请按时间核查 processing。")
            job = result["job"]
            if job is not None:
                if claim_audit is not None and isinstance(job, dict):
                    entry = {"server_url": client.url, "observed_at_utc": datetime.now(timezone.utc).isoformat()}
                    for field in ("job_id", "msg_id", "claimed_at"):
                        value = job.get(field)
                        entry[field] = value if isinstance(value, str) else None
                    claim_audit.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    claim_audit.flush()
                if not isinstance(job, dict) or not isinstance(job.get("text"), str):
                    raise ProbeError("任务格式异常，请按时间核查 processing。")
                if not re.match(r"^B2_TEST(?:\s|$)", job["text"]):
                    raise ProbeError("领到非 B2_TEST 任务，已停止，请按时间人工核查 processing。")
                msg_id = job.get("msg_id")
                if not isinstance(msg_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", msg_id):
                    raise ProbeError("测试任务缺少可用的 MsgId，请核查 processing。")
                log("已领取 B2_TEST 测试任务。")
                return msg_id
            log("Worker 鉴权通过，队列为空。")
        time.sleep(INTERVAL)


def complete(client, token, msg_id):
    path = f"/api/jobs/{msg_id}/complete"
    while True:
        try:
            result = client.request(path, token, RECEIPT)
        except (Unavailable, StatusError) as exc:
            if isinstance(exc, StatusError) and not transient(exc.status):
                raise
            log("complete 状态待确认，15 秒后重传同一回执。")
            time.sleep(INTERVAL)
            continue
        if (result.get("status") != "done" or result.get("exec_ok") is not True
                or "operation_id" not in result or result["operation_id"] is not None
                or not isinstance(result.get("already_completed"), bool)):
            raise ProbeError("服务端持久化结果与模拟回执不符，请人工核查。")
        return result


def run(client, token, claim_audit=None):
    check_health(client)
    reject_invalid_token(client)
    msg_id = wait_for_job(client, token, claim_audit)
    first = complete(client, token, msg_id)
    if first["already_completed"]:
        log("服务端确认此前已保存该回执。")
    duplicate = complete(client, token, msg_id)
    if not duplicate["already_completed"]:
        raise ProbeError("重复提交未返回 already_completed=true，请核查微信通知次数。")
    log("同一回执重复提交已获确认。")
    status = duplicate.get("notify_status")
    if status in ("sent", "success", "ok", "delivered"):
        log(f"服务端报告微信通知状态为 {status}。请在微信核对仅收到一次模拟回执。")
    elif status in ("failed", "error", "pending", "skipped"):
        raise ProbeError(f"模拟回执已保存，微信通知状态为 {status}。")
    else:
        raise ProbeError("模拟回执已保存，微信通知状态未知。请核查云端记录与微信。")


def main(argv=None):
    parser = argparse.ArgumentParser(description="独立微信 B2 探针；默认只做公共健康检查。")
    parser.add_argument("--isolated-test-queue", action="store_true",
                        help="确认其他 Worker 已停止且无真实 pending 请求，启用领取和模拟回执")
    parser.add_argument("--claim-audit", type=Path, metavar="FILE",
                        help="在指定的新建本地文件中记录服务器根地址和领取定位字段")
    args = parser.parse_args(argv)
    if args.claim_audit and not args.isolated_test_queue:
        parser.error("--claim-audit 需要 --isolated-test-queue")
    try:
        client = Client(os.environ.get("CALENDAR_SERVER_URL", ""))
        if args.isolated_test_queue:
            token = os.environ.get("CALENDAR_WORKER_TOKEN", "")
            if not token or not token.isascii() or any(not 33 <= ord(c) <= 126 for c in token):
                raise ProbeError("请在本机配置有效的 CALENDAR_WORKER_TOKEN。")
            if args.claim_audit:
                try:
                    with args.claim_audit.open("x", encoding="utf-8") as claim_audit:
                        run(client, token, claim_audit)
                except OSError:
                    raise ProbeError("无法写入本地领取记录。如已发出 claim，请核查 processing。") from None
            else:
                run(client, token)
        else:
            check_health(client)
            log("Worker 身份尚未验证。")
        return 0
    except KeyboardInterrupt:
        log("探针已退出。如中断时正在领取或提交，请按 UTC 时间核查云端记录。")
        return 130
    except (StatusError, ProbeError, ValueError) as exc:
        log(str(exc))
    except Unavailable:
        log("未收到有效响应，请检查网络、代理、TLS 和服务状态。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
