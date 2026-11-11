"""``jsonl`` 去向：本地文件，一次检索一行（#102）。

它是两种**轻去向**之一（ADR-0021 决策 4「代价」最后一条）：没有观测栈的时候仍然能
回答「哪一步慢」，代价是没有查询与聚合，只能 ``grep``。

四条形态约束，每一条都对应一个真实的失败方式：

1. **一次检索一行**。六段耗时在同一行里；拆成六行的话，「这次慢在哪一步」要先做一次
   关联，而文件格式本身没有能给关联用的键。
2. **单行 ≤ 2 KB**。读它的是 ``grep`` 与磁盘水位。属性值再长也只截短——这一行承载的是
   身份与耗时，不是内容。
3. **按天轮转**。文件名里带日期（UTC）。轮转粒度是天，因为要回答的问题是「最近几天」，
   而不是「第几个 100 MB」。
4. **定期清理 + 只读最近 N 天**。保留天数进配置（``TRACE_JSONL_RETENTION_DAYS``）：
   写的时候顺手清掉超期文件，读的时候也只打开窗口内的那些。少了一半，磁盘或查询就会
   由这张单子自己撑爆。

**这里不存原文**：行里只有 ``trace_id``、结构化事实（模式、条数、降级原因、空结果原因）
与六段耗时。问句、改写句、切片正文一个字都不进——它们是 ADR-0021 决策 1 划给内网观测栈
的东西，而本去向恰恰是「没有栈」时的降级去处，更不该在磁盘上留一份原文。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

import structlog

from app.observability.stage_timing import StageSpan

logger = structlog.get_logger(__name__)

#: 单行字节上限。契约写的是「≤ 2 KB」，所以它是常量而不是配置：调大它等于改契约。
MAX_LINE_BYTES: Final[int] = 2048

#: 文件名前缀 / 后缀。清理与读取都只认这个形状——误配目录时也只会碰自己写的那类文件。
FILE_PREFIX: Final[str] = "retrieval-traces-"
FILE_SUFFIX: Final[str] = ".jsonl"
_DATE_FORMAT: Final[str] = "%Y-%m-%d"

#: 行里由本模块占用的键。属性若与它们同名，属性让路（身份与耗时优先）。
_RESERVED_KEYS: Final[frozenset[str]] = frozenset({"ts", "trace_id", "spans", "truncated"})

#: 超长时属性的字符上限。选 64：够放下 ``mode`` / 原因串一类的枚举值，放不下正文。
_ATTRIBUTE_VALUE_MAX_CHARS: Final[int] = 64


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class JsonlTraceSink:
    """把每条 trace 落成文件里的一行；缓冲在内存里，``flush`` 时写盘。

    为什么是「缓冲到 flush 再写」而不是逐段追加：一次检索一行这条约束要求六段凑齐了
    才能落盘。缓冲按 ``trace_id`` 分开，``flush`` 只弹自己那一条——两次并发检索因此
    不会互相写半行。
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        retention_days: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._directory = Path(directory)
        # 下界夹到 1 天：配置成 0 或负数时「保留」就变成了「清空」，那是配置事故而不是功能。
        self._retention_days = max(int(retention_days), 1)
        self._clock = clock or _utc_now
        self._pending: dict[str, dict[str, Any]] = {}

    # ── 协议 ──
    def record_trace(
        self,
        trace_id: str,
        attributes: Mapping[str, object],
        *,
        query: str | None = None,
    ) -> None:
        """记下这次检索的结构化事实；真正的落盘在 ``flush``。

        ``query`` **收下但丢掉**（#110 复盘后显式化的决定）：这个文件是给人 grep 的
        「哪一步慢」，不是低分样本导出的取数源——导出只认观测栈。原文一旦落进本地文件，
        它就跟着备份 / 排查包一起走，而红线是「原文不进业务库、也不进随手能带的副本」。
        参数照协议收，是为了让「去向决定原文去留」只有一处判断（见 ``sink.py``）。
        """

        self._pending[trace_id] = {
            "ts": self._clock(),
            "attributes": dict(attributes),
            "spans": [],
        }

    def record_span(self, trace_id: str, name: str, duration_ms: float) -> None:
        """往这条 trace 上挂一段耗时。没见过 ``record_trace`` 的 id 直接忽略。"""

        pending = self._pending.get(trace_id)
        if pending is None:
            return
        pending["spans"].append(StageSpan(name=name, duration_ms=duration_ms))

    def record_score(self, trace_id: str, score: int, *, comment: str | None = None) -> None:
        """**丢弃**：分数在 ``retrieval_feedback`` 表里，本文件不是分数台账。

        而且这里也没法「补写」——一行在 ``flush`` 时就落盘了，而反馈往往晚几秒甚至几天
        才来。真要把它塞进去就得重写已落盘的行，那会让「一次检索一行」这条形状失效。
        """

        return None

    def flush(self, trace_id: str) -> None:
        """写一行，并按保留天数清掉超期文件；没见过这条 trace 时什么也不做。"""

        pending = self._pending.pop(trace_id, None)
        if pending is None:
            return

        line = self._line_of(trace_id, pending)
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._path_of(pending["ts"].date())
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        self._prune(today=pending["ts"].date())

    # ── 行 ──
    def _line_of(self, trace_id: str, pending: Mapping[str, Any]) -> str:
        """构造这一行；超过上限时先截短属性值，再不行就把属性整批摘掉。

        截短是**有损**的，所以往行里写一个 ``truncated: true``：读到它的人必须知道
        这一行的属性不全，否则会以为自己看到的就是全部事实。
        """

        record = self._record_of(trace_id, pending, max_chars=None)
        line = _dump(record)
        if len(line.encode("utf-8")) <= MAX_LINE_BYTES:
            return line

        record = self._record_of(trace_id, pending, max_chars=_ATTRIBUTE_VALUE_MAX_CHARS)
        record["truncated"] = True
        line = _dump(record)
        if len(line.encode("utf-8")) <= MAX_LINE_BYTES:
            return line

        # 兜底：属性整批摘掉。身份与六段耗时是这一行的意义所在，先保它们。
        return _dump(
            {
                "ts": record["ts"],
                "trace_id": trace_id,
                "spans": record["spans"],
                "truncated": True,
            }
        )

    @staticmethod
    def _record_of(
        trace_id: str, pending: Mapping[str, Any], *, max_chars: int | None
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "ts": pending["ts"].isoformat(),
            "trace_id": trace_id,
        }
        for key, value in pending["attributes"].items():
            if key in _RESERVED_KEYS or key in record:
                continue
            if max_chars is not None and isinstance(value, str) and len(value) > max_chars:
                record[key] = value[:max_chars] + "…"
                continue
            record[key] = value
        record["spans"] = [
            {"name": span.name, "duration_ms": span.duration_ms} for span in pending["spans"]
        ]
        return record

    # ── 文件 ──
    def _path_of(self, day: date) -> Path:
        return self._directory / f"{FILE_PREFIX}{day.strftime(_DATE_FORMAT)}{FILE_SUFFIX}"

    def _prune(self, *, today: date) -> None:
        """清掉早于保留窗口的文件；只认自己的命名形状。"""

        earliest = today - timedelta(days=self._retention_days - 1)
        for path in self._directory.glob(f"{FILE_PREFIX}*{FILE_SUFFIX}"):
            day = _day_of(path.name)
            if day is None or day >= earliest:
                continue
            try:
                path.unlink()
            except OSError as exc:
                # 清不掉只有一个后果：这个文件多留一天。清理失败不影响这一行已经写下的记录，
                # 也不该把异常抛回检索——门面那一层会把它变成 warning，但那是兜底，不是设计。
                logger.warning(
                    "trace jsonl could not be pruned",
                    file=path.name,
                    error=type(exc).__name__,
                )


def read_recent_records(
    directory: str | Path, *, days: int, today: date | None = None
) -> list[dict[str, Any]]:
    """按天读取窗口内的记录（今天是窗口最后一天）；更早的文件一概不打开。

    这条读取窗口是给控制台读回通道（#103）与排障脚本用的：JSONL 没有索引，能依赖的
    只有文件名里的日期，所以「只读最近 N 天」必须由这里保证，而不是由调用方自觉。
    坏行（例如进程被杀时写了一半）跳过并留一条 warning——一个残行不该让整次读取失败。
    """

    base = Path(directory)
    if not base.is_dir():
        return []
    window = max(int(days), 1)
    end = today or _utc_now().date()
    earliest = end - timedelta(days=window - 1)

    records: list[dict[str, Any]] = []
    for path in sorted(base.glob(f"{FILE_PREFIX}*{FILE_SUFFIX}")):
        day = _day_of(path.name)
        if day is None or not earliest <= day <= end:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning(
                    "trace jsonl line is not valid json",
                    file=path.name,
                    line=number,
                )
    return records


def _day_of(filename: str) -> date | None:
    """从文件名里取出日期；形状不认识就返回 ``None``（不去猜）。"""

    if not filename.startswith(FILE_PREFIX) or not filename.endswith(FILE_SUFFIX):
        return None
    stamp = filename[len(FILE_PREFIX) : -len(FILE_SUFFIX)]
    try:
        return datetime.strptime(stamp, _DATE_FORMAT).date()
    except ValueError:
        return None


def _dump(record: Mapping[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
