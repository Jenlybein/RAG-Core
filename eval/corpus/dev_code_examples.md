# 开发者接口示例与运维脚本

本页收录售后工单、证书轮换与回调排障时复用的脚本片段和接口样例。代码块里的函数名、字段名与错误码都是检索用的判别信号，不是占位符，改动脚本时要同步改这里。有语言标记的围栏按语法分隔符切，无语言标记的围栏回退到正文分隔符，两类片段放在一起才能对照切块器的处理差异。

## 售后工单的自动催办

### 催办脚本

`escalate_support_ticket` 按 `failure_reason` 分流待催办工单：限流类错误走退避，回调超时走重投，其余转人工。`retry_refund_callback` 只消费 `retry_callback` 这一种决定，不重试签名不匹配。

```python
import time
from collections.abc import Iterable

import requests

BASE_URL = "https://api.example.com/api/v1"
RETRY_BACKOFF = (10, 30, 90)
RETRYABLE_REASONS = {"callback_timeout", "embedding_timeout"}


def list_pending_refunds(kb_id: str) -> list[dict]:
    """列出退款工单里仍待回调确认的记录。"""
    response = requests.get(
        f"{BASE_URL}/support/refunds",
        params={"kb_id": kb_id, "status": "pending_callback"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["data"]["refunds"]


def escalate_support_ticket(ticket: dict) -> dict[str, str]:
    """按 failure_reason 与 error_code 给出催办动作。"""
    reason = ticket.get("failure_reason", "")
    if ticket.get("error_code") == "E2048":
        return {
            "action": "backoff",
            "retry_after_seconds": "60",
            "error_code": "E2048",
            "failure_reason": "minute_quota_exceeded",
        }
    if reason in RETRYABLE_REASONS:
        return {
            "action": "retry_callback",
            "error_code": "E3116",
            "dedupe_key": ticket["event_id"],
        }
    return {"action": "manual_review", "failure_reason": reason or "unknown"}


def retry_refund_callback(refunds: Iterable[dict]) -> dict[str, int]:
    """逐条重投回调，统计成功、跳过与最终失败。"""
    stats = {"ok": 0, "skip": 0, "failed": 0}
    for refund in refunds:
        decision = escalate_support_ticket(refund)
        if decision["action"] != "retry_callback":
            stats["skip"] += 1
            continue
        for delay in RETRY_BACKOFF:
            receipt = requests.post(
                f"{BASE_URL}/support/refunds/{refund['refund_id']}/retry",
                json={"dedupe_key": decision["dedupe_key"]},
                timeout=60,
            )
            if receipt.json().get("code") == 0:
                stats["ok"] += 1
                break
            time.sleep(delay)
        else:
            stats["failed"] += 1
    return stats
```

### 退款回调请求体

回调体里的 `event_id` 是幂等键，`failure_reason` 与 `error_code` 决定催办脚本走哪条分支。`extra` 下的扩展字段允许增加，调用方要忽略不认识的值。

```json
{
  "event_id": "evt_refund_9c41",
  "event_type": "refund.rejected",
  "occurred_at": "2026-11-09T12:05:31+08:00",
  "data": {
    "refund_id": "RF-20261109-0031",
    "kb_id": "kb-eval",
    "amount_cents": 128000,
    "failure_reason": "callback_timeout",
    "error_code": "E3116",
    "retry_after_seconds": 30
  },
  "extra": {
    "signature_version": "v2",
    "dedupe_key": "RF-20261109-0031"
  }
}
```

## 证书与回调的值班脚本

### 心跳检查与续期

`check_device_heartbeat` 把心跳响应归一成值班记录，`renew_certificate` 再按 `error_code` 决定是升级固件还是切换密钥版本。`E1041` 与 `E3112` 的处置不同，不能合并成「重新续期」。

```python
from dataclasses import dataclass

CERT_ERRORS = {
    "E1041": "unsupported_signature_algorithm",
    "E3112": "key_version_mismatch",
}


@dataclass(frozen=True)
class HeartbeatReport:
    device_id: str
    status: str
    failure_reason: str
    error_code: str


def check_device_heartbeat(device_id: str, payload: dict) -> HeartbeatReport:
    """把心跳响应归一成值班记录，保留原始错误码供检索。"""
    return HeartbeatReport(
        device_id=device_id,
        status=payload.get("status", "unknown"),
        failure_reason=payload.get("failure_reason", ""),
        error_code=payload.get("error_code", ""),
    )


def renew_certificate(report: HeartbeatReport) -> dict[str, str]:
    """E1041 先升固件再续期；E3112 只需切密钥版本。"""
    if report.error_code == "E1041":
        return {
            "action": "upgrade_firmware_then_renew",
            "error_code": "E1041",
            "failure_reason": CERT_ERRORS["E1041"],
        }
    if report.error_code == "E3112":
        return {
            "action": "rotate_key_version",
            "error_code": "E3112",
            "failure_reason": CERT_ERRORS["E3112"],
        }
    return {
        "action": "online_renewal",
        "error_code": report.error_code,
        "failure_reason": report.failure_reason,
    }
```

### 降级检索响应

`degraded_reason` 只允许固定枚举，`scores_vector` 与 `score` 不是同一个量。检索接口在关键词通道不可用时仍返回结果，调用方要读 `outcome` 而不是只看 `code`。

```json
{
  "code": 0,
  "data": {
    "outcome": "degraded",
    "degraded_reason": "keyword_unavailable",
    "query": "证书续期后仍报签名算法不支持怎么处置",
    "kb_ids": ["kb-eval", "kb-eval-ops"],
    "results": [
      {
        "chunk_id": "chunk_cert_0142",
        "score": 0.83,
        "heading_path": "值班处置手册 > 证书轮换操作步骤",
        "chunk_type": "prose"
      }
    ],
    "scores_vector": [0.83],
    "scores_keyword": []
  }
}
```

## 无标记片段与排障记录

### 重建索引命令

下面这段没有语言标记：值班先强制重建 `doc_7f3a`，再看 `status`、`chunk_count` 与 `failure_reason`，最后从 Worker 日志里捞 `E3116` 与 `E2048`。

```
curl -X POST "http://127.0.0.1:8000/api/v1/documents/doc_7f3a/reindex?force=true" \
  -H "X-Request-Id: ops-cert-20261109" \
  -d '{"reparse": false, "reason": "certificate rotation"}'
curl -s "http://127.0.0.1:8000/api/v1/documents/doc_7f3a" \
  | jq '.data.status, .data.chunk_count, .data.failure_reason'
systemctl status rag-worker
journalctl -u rag-worker --since "-30 min" | grep 'E3116\|E2048'
```

### 失败任务日志

日志片段同样不带语言标记，却含 `reindex_document`、`retry_refund_callback` 两个函数名与 `failure_reason`、`error_code`、`degraded_reason` 三个字段名。排查时按 `dedupe_key` 把重投记录串起来，不要把 `status=5` 当成还在处理中。

```
2026-11-09 21:14:07 ERROR worker=rag-worker-02 queue=indexing task=reindex_document
document_id=doc_7f3a status=5 failure_reason=certificate_chain_expired error_code=E1041
retry_refund_callback failed event_id=evt_refund_9c41 error_code=E3116 dedupe_key=RF-20261109-0031
GET /api/v1/rag/retrieve outcome=degraded degraded_reason=keyword_unavailable top_k=5
POST /api/v1/support/refunds/RF-20261109-0031/retry code=0 action=retry_callback
```
