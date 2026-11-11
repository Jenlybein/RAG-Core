"""#102 · 六段计时与三种去向：收集器、协议、三个实现。

接缝都是**外部可观察**的：

* :class:`StageTimer`：显式传递的收集器，「六段名字与顺序」「没发生的段不出现」由它守；
* 三个实现的协议方法（``record_trace`` → ``record_span`` → ``flush``）：各自落地成
  文件、内存或 SDK 调用，测试只看它们**对外**留下了什么（文件里的行、SDK 收到的参数）；
* :func:`app.observability.emit_retrieval_trace`：唯一一处把异常吞成 ``warning`` 的地方。

套件不发网络、不连 PG：langfuse 实现全程对着一个记录用假客户端，JSONL 实现写在
``tmp_path`` 里。整条检索链路上的那一半（一次检索一行、不含原文、响应逐字节一致）
见 ``tests/test_retrieval_observability.py``。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

import pytest

from app.core.config import SUPPORTED_TRACE_SINKS, settings
from app.observability import (
    emit_retrieval_trace,
    get_trace_sink,
    set_trace_sink,
)
from app.observability.jsonl_sink import MAX_LINE_BYTES, JsonlTraceSink, read_recent_records
from app.observability.langfuse_sink import LangfuseTraceSink
from app.observability.sink import NoneTraceSink
from app.observability.stage_timing import (
    STAGE_COLLAPSE,
    STAGE_FUSION,
    STAGE_KEYWORD_RECALL,
    STAGE_QUERY_OPTIMIZATION,
    STAGE_RERANK,
    STAGE_VECTOR_RECALL,
    STAGE_ORDER,
    StageSpan,
    StageTimer,
)

# ──────────────────────────────────────────────── 六段的词表与顺序：契约，不是实现细节

SIX_STAGES = [
    STAGE_QUERY_OPTIMIZATION,
    STAGE_VECTOR_RECALL,
    STAGE_KEYWORD_RECALL,
    STAGE_FUSION,
    STAGE_RERANK,
    STAGE_COLLAPSE,
]


def test_the_six_stages_are_the_ones_the_contract_names() -> None:
    """六段名字写死在契约里（GLOSSARY「span 阶段」）：观测栈按名字聚合，措辞不能漂。"""

    assert list(STAGE_ORDER) == SIX_STAGES
    assert SIX_STAGES == [
        "query_optimization",
        "vector_recall",
        "keyword_recall",
        "fusion",
        "rerank",
        "collapse",
    ]


# ──────────────────────────────────────────────────────────── 收集器：显式传递、无全局状态


def test_a_stage_is_timed_by_the_collector_it_is_given() -> None:
    """计时收集器是**传进来的那个对象**，不是全局单例：两个实例互不串味。"""

    first = StageTimer()
    second = StageTimer()

    with first.measure(STAGE_VECTOR_RECALL):
        pass

    assert [span.name for span in first.spans()] == [STAGE_VECTOR_RECALL]
    assert second.spans() == ()
    assert isinstance(first.spans()[0], StageSpan)
    assert first.spans()[0].duration_ms >= 0.0


def test_stages_are_reported_in_chain_order_not_finish_order() -> None:
    """两路召回是并行的：谁先跑完不构成事实，报告顺序按链路固定序。

    否则同一次检索两次运行的 JSONL 行内容会差一个顺序，测试与排障都得先排序再比。
    """

    timer = StageTimer()
    timer.record(STAGE_COLLAPSE, 0.4)
    timer.record(STAGE_KEYWORD_RECALL, 0.2)
    timer.record(STAGE_QUERY_OPTIMIZATION, 0.1)

    assert [span.name for span in timer.spans()] == [
        STAGE_QUERY_OPTIMIZATION,
        STAGE_KEYWORD_RECALL,
        STAGE_COLLAPSE,
    ]


def test_a_stage_that_blew_up_is_still_timed() -> None:
    """失败的那一段也花了时间：把它从计量里抹掉，等于让「哪一步慢」少了最该看的一行。"""

    timer = StageTimer()

    with pytest.raises(RuntimeError):
        with timer.measure(STAGE_KEYWORD_RECALL):
            raise RuntimeError("engine down")

    assert [span.name for span in timer.spans()] == [STAGE_KEYWORD_RECALL]


# ─────────────────────────────────────────────────────── jsonl：一次检索一行、按天轮转

TRACE_ID = "0123456789abcdef0123456789abcdef"

#: 一次检索交给观测层的结构化事实（组合根给的就是这四个键）。
ATTRIBUTES: dict[str, object] = {
    "mode": "hybrid",
    "top_k": 5,
    "degraded_reason": None,
    "empty_reason": None,
}

#: 六段耗时：值不重要，形状与顺序才重要。
SIX_SPANS: list[tuple[str, float]] = [
    (STAGE_QUERY_OPTIMIZATION, 1.25),
    (STAGE_VECTOR_RECALL, 12.5),
    (STAGE_KEYWORD_RECALL, 9.0),
    (STAGE_FUSION, 0.5),
    (STAGE_RERANK, 0.0),
    (STAGE_COLLAPSE, 3.75),
]


def _fixed_clock(day: str) -> Any:
    """一个固定在某一天的时钟：轮转与保留期都要能在测试里被钉住，不靠系统时间。"""

    moment = datetime.fromisoformat(f"{day}T08:30:00+00:00")

    def clock() -> datetime:
        return moment

    return clock


def _write_trace(sink: Any, trace_id: str = TRACE_ID) -> None:
    """走一遍协议的三步：记录 trace → 逐段记 span → flush。"""

    sink.record_trace(trace_id, ATTRIBUTES)
    for name, duration_ms in SIX_SPANS:
        sink.record_span(trace_id, name, duration_ms)
    sink.flush(trace_id)


def _lines(directory: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(directory.glob("retrieval-traces-*.jsonl")):
        records.extend(
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line
        )
    return records


#: 契约写死的单行上限（「单行 ≤ 2 KB」）。这里写**字面值**而不是引用模块常量：
#: 用实现自己的常量去比实现自己的产出，是「怎么改都对」的那种断言。
LINE_CAP_BYTES = 2048


def test_the_line_cap_is_two_kilobytes() -> None:
    """上限是契约常量，不是一个可调的配置项：调大它等于改契约。"""

    assert MAX_LINE_BYTES == LINE_CAP_BYTES


def test_one_retrieval_and_every_stage_spans_exactly_one_line(tmp_path: Path) -> None:
    """六段耗时在同一行里：查「这次慢在哪一步」不该要求先把六行拼起来。"""

    sink = JsonlTraceSink(tmp_path, retention_days=7, clock=_fixed_clock("2026-11-11"))
    _write_trace(sink)

    files = sorted(tmp_path.glob("retrieval-traces-*.jsonl"))
    assert [path.name for path in files] == ["retrieval-traces-2026-11-11.jsonl"], (
        "按天轮转：文件名里就是这一行的日期"
    )
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, "一次检索一行"

    record = json.loads(lines[0])
    assert set(record) == {
        "ts",
        "trace_id",
        "mode",
        "top_k",
        "degraded_reason",
        "empty_reason",
        "spans",
    }, "行里只有结构化事实，没有留给正文的字段"
    assert record["trace_id"] == TRACE_ID
    assert record["mode"] == "hybrid"
    assert record["top_k"] == 5
    assert [(span["name"], span["duration_ms"]) for span in record["spans"]] == SIX_SPANS


def test_two_retrievals_are_two_lines(tmp_path: Path) -> None:
    """每次 flush 落一行；buffer 不许把上一次的内容带进下一次。"""

    sink = JsonlTraceSink(tmp_path, retention_days=7, clock=_fixed_clock("2026-11-11"))
    _write_trace(sink, "a" * 32)
    _write_trace(sink, "b" * 32)

    records = _lines(tmp_path)
    assert [record["trace_id"] for record in records] == ["a" * 32, "b" * 32]


def test_a_line_never_exceeds_the_size_cap(tmp_path: Path) -> None:
    """单行 ≤ 2 KB：属性值再长也只能被截短，不能让一行长成一片。

    这条上限不是审美：JSONL 去向是**降级去向**，读它的是 ``grep`` 和磁盘水位，
    一行几兆的垃圾记录会把这两件事同时弄坏。
    """

    sink = JsonlTraceSink(tmp_path, retention_days=7, clock=_fixed_clock("2026-11-11"))
    sink.record_trace(TRACE_ID, {**ATTRIBUTES, "mode": "x" * 10_000})
    for name, duration_ms in SIX_SPANS:
        sink.record_span(TRACE_ID, name, duration_ms)
    sink.flush(TRACE_ID)

    raw = next(tmp_path.glob("retrieval-traces-*.jsonl")).read_bytes()
    line = raw.split(b"\n")[0]
    assert len(line) <= LINE_CAP_BYTES, f"一行 {len(line)} 字节，超过上限"

    record = json.loads(line)
    assert record["trace_id"] == TRACE_ID, "被截的只能是属性值，身份必须完好"
    assert [span["name"] for span in record["spans"]] == [name for name, _ in SIX_SPANS], (
        "六段耗时不能因为是长行就被丢掉"
    )
    assert record.get("truncated") is True, "截断这件事要写在行里，不能静默"


def test_files_rotate_by_day(tmp_path: Path) -> None:
    """按天轮转：换一天就换一个文件，旧文件不被追加写。"""

    first_day = JsonlTraceSink(tmp_path, retention_days=7, clock=_fixed_clock("2026-11-10"))
    _write_trace(first_day, "a" * 32)
    next_day = JsonlTraceSink(tmp_path, retention_days=7, clock=_fixed_clock("2026-11-11"))
    _write_trace(next_day, "b" * 32)

    names = sorted(path.name for path in tmp_path.glob("retrieval-traces-*.jsonl"))
    assert names == ["retrieval-traces-2026-11-10.jsonl", "retrieval-traces-2026-11-11.jsonl"]
    assert [record["trace_id"] for record in _lines(tmp_path)] == ["a" * 32, "b" * 32]


def test_files_older_than_the_retention_window_are_dropped(tmp_path: Path) -> None:
    """保留天数要**真的**生效：只保留最近 N 天，否则磁盘由这张单子自己撑爆。"""

    (tmp_path / "retrieval-traces-2026-10-01.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "unrelated-2026-10-01.jsonl").write_text("leave me\n", encoding="utf-8")

    sink = JsonlTraceSink(tmp_path, retention_days=3, clock=_fixed_clock("2026-11-11"))
    _write_trace(sink)

    names = sorted(path.name for path in tmp_path.iterdir())
    assert "retrieval-traces-2026-10-01.jsonl" not in names, "超期文件要被清掉"
    assert "unrelated-2026-10-01.jsonl" in names, "只清理自己写的那类文件，绝不碰别人的"
    assert "retrieval-traces-2026-11-11.jsonl" in names


def test_reading_only_covers_the_recent_days(tmp_path: Path) -> None:
    """读取窗口按天算：更早的文件一概不打开（读它既慢又答非所问）。"""

    for day in ("2026-11-11", "2026-11-09", "2026-11-08", "2026-10-01"):
        (tmp_path / f"retrieval-traces-{day}.jsonl").write_text(
            json.dumps({"trace_id": day}) + "\n", encoding="utf-8"
        )

    records = read_recent_records(tmp_path, days=3, today=date(2026, 11, 11))

    assert [record["trace_id"] for record in records] == ["2026-11-09", "2026-11-11"]


# ─────────────────────────────────────────────── none：不落文件、不建连接、什么都不做


def test_the_none_sink_touches_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """「丢弃」去向必须是**真的**丢弃：目录配了也不该被创建，更不该有文件。"""

    monkeypatch.setattr(settings, "TRACE_SINK", "none")
    monkeypatch.setattr(settings, "TRACE_JSONL_DIR", str(tmp_path / "traces"))

    _write_trace(get_trace_sink())

    assert not (tmp_path / "traces").exists()
    assert list(tmp_path.iterdir()) == []


# ───────────────────────────────────────────────────── langfuse：注入我们的 trace_id


class _RecordingLangfuseClient:
    """记录用假客户端：把 SDK 收到的参数原样存下来，一次网络都不发。"""

    def __init__(self) -> None:
        self.observations: list[dict[str, Any]] = []
        self.scores: list[dict[str, Any]] = []
        self.flushes = 0

    def start_as_current_observation(self, **kwargs: Any) -> Any:
        self.observations.append(kwargs)
        return _NullContext()

    def create_score(self, **kwargs: Any) -> None:
        self.scores.append(kwargs)

    def flush(self) -> None:
        self.flushes += 1


class _NullContext:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: object) -> bool:
        return False


class _ExplodingLangfuseClient:
    """服务不可达的替身：SDK 一碰就抛。"""

    def start_as_current_observation(self, **_kwargs: Any) -> Any:
        raise RuntimeError("langfuse is unreachable")


def test_langfuse_spans_carry_the_trace_id_we_derived() -> None:
    """``trace_context`` 里必须是**我们派生的那个 id**，不是 SDK 另生成的一个。

    注入形状是这一层唯一能证明的事：``as_type="span"``、名字取自六段常量、六段各自带
    真实耗时（``metadata.duration_ms``——它们是事后补记的，SDK 自己量到的那段接近 0）。
    """

    client = _RecordingLangfuseClient()

    _write_trace(LangfuseTraceSink(client=client))

    assert [call["name"] for call in client.observations] == [name for name, _ in SIX_SPANS]
    assert [call["as_type"] for call in client.observations] == ["span"] * len(SIX_SPANS)
    assert [call["trace_context"] for call in client.observations] == [
        {"trace_id": TRACE_ID}
    ] * len(SIX_SPANS)
    assert [call["metadata"]["duration_ms"] for call in client.observations] == [
        duration_ms for _, duration_ms in SIX_SPANS
    ]
    assert [call["metadata"]["mode"] for call in client.observations] == ["hybrid"] * len(SIX_SPANS)
    assert client.flushes == 1


# ───────────────────────────────────────── 门面：一个配置项切去向，异常只留 warning


def _records(log_dir: Path, filename: str = "app.log") -> list[dict[str, Any]]:
    path = log_dir / filename
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture()
def log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """每条测试一个干净的临时日志目录（与 ``test_trace_identity`` 同一套做法）。"""

    import importlib
    import logging

    log_dir = tmp_path / "logs"
    logging_mod = importlib.import_module("app.core.logging")
    config_mod = importlib.import_module("app.core.config")
    monkeypatch.setattr(config_mod.settings, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(config_mod.settings, "LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(logging_mod, "_configured", False)
    logging_mod.setup_logging()
    yield log_dir
    logging.shutdown()
    logging_mod._configured = False
    logging_mod.setup_logging(log_dir=tmp_path / "logs-restored", log_level="INFO")


def test_the_sink_comes_from_one_config_item(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """三个去向由 ``TRACE_SINK`` 一个值切换：链路代码不认识这个值。"""

    assert set(SUPPORTED_TRACE_SINKS) == {"none", "jsonl", "langfuse"}, (
        "词表就是这三个去向，多一个等于多一条没人实现的配置"
    )

    monkeypatch.setattr(settings, "TRACE_JSONL_DIR", str(tmp_path))

    monkeypatch.setattr(settings, "TRACE_SINK", "none")
    assert isinstance(get_trace_sink(), NoneTraceSink)

    monkeypatch.setattr(settings, "TRACE_SINK", "jsonl")
    assert isinstance(get_trace_sink(), JsonlTraceSink)

    monkeypatch.setattr(settings, "TRACE_SINK", "langfuse")
    sink = get_trace_sink()
    assert isinstance(sink, LangfuseTraceSink)
    assert sink._client is None, "换去向不该在启动路径上连观测栈"


def test_a_sink_that_raises_only_leaves_one_warning(log_dir: Path) -> None:
    """观测服务连不上：只留一条 warning，一次都不许往外抛。"""

    set_trace_sink(LangfuseTraceSink(client=_ExplodingLangfuseClient()))

    emit_retrieval_trace(
        trace_id=TRACE_ID,
        spans=tuple(StageSpan(name=name, duration_ms=ms) for name, ms in SIX_SPANS),
        attributes=ATTRIBUTES,
    )

    warnings = [
        record
        for record in _records(log_dir)
        if record.get("msg") == "observability trace not recorded"
    ]
    assert len(warnings) == 1, "一次失败只留一条 warning"
    assert warnings[0]["level"] == "warning"
    assert warnings[0]["trace_id"] == TRACE_ID, "warning 里要能看出是哪一次"
    assert not _records(log_dir, "error.log"), "旁路失败不是 ERROR"


# ──────────────────────────────────────── 原文与分数：去向决定收不收（#110 / #105 复盘）

SENTINEL_QUERY = "退款申请提交后几天能到账（哨兵 9f3b）"


def test_the_langfuse_sink_carries_the_query_in_every_span(tmp_path: Path) -> None:
    """原文只进观测栈：低分样本导出要靠它把差评变回题目（#110）。

    v5 没有 trace 级 IO setter，六段又是并列的兄弟 span，所以写进**每一段**的 metadata。
    重复六份是为让导出器读任意一段都拿得到。
    """

    client = _RecordingLangfuseClient()
    sink = LangfuseTraceSink(client=client)

    sink.record_trace(TRACE_ID, ATTRIBUTES, query=SENTINEL_QUERY)
    for name, duration_ms in SIX_SPANS:
        sink.record_span(TRACE_ID, name, duration_ms)

    assert [obs["metadata"]["query"] for obs in client.observations] == [SENTINEL_QUERY] * len(SIX_SPANS)


def test_the_jsonl_sink_drops_the_query(tmp_path: Path) -> None:
    """本地文件**进不去原文**：它会跟着备份与排查包一起走，而导出只认观测栈。"""

    directory = tmp_path / "traces"
    sink = JsonlTraceSink(str(directory), retention_days=7)

    sink.record_trace(TRACE_ID, ATTRIBUTES, query=SENTINEL_QUERY)
    for name, duration_ms in SIX_SPANS:
        sink.record_span(TRACE_ID, name, duration_ms)
    sink.flush(TRACE_ID)

    raw = "".join(path.read_text(encoding="utf-8") for path in directory.glob("*.jsonl"))
    assert SENTINEL_QUERY not in raw
    assert "9f3b" not in raw, "连哨兵片段都不该出现，否则是部分泄漏"


def test_a_score_is_pushed_to_the_langfuse_sink_and_dropped_by_jsonl(tmp_path: Path) -> None:
    """分数推送是旁路（#105 决策 2）：栈里成一条 score，本地文件不收。"""

    client = _RecordingLangfuseClient()
    sink = LangfuseTraceSink(client=client)

    sink.record_score(TRACE_ID, 2, comment="答非所问")
    sink.record_score(TRACE_ID, 4)

    assert client.scores == [
        {"trace_id": TRACE_ID, "name": "user_feedback", "value": 2, "comment": "答非所问"},
        {"trace_id": TRACE_ID, "name": "user_feedback", "value": 4, "comment": None},
    ]

    directory = tmp_path / "traces"
    jsonl = JsonlTraceSink(str(directory), retention_days=7)
    jsonl.record_score(TRACE_ID, 2)
    jsonl.flush(TRACE_ID)
    assert list(directory.glob("*.jsonl")) == []
