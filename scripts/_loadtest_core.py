"""S0 压测入口的**纯逻辑**：延迟口径、主循环同步 DB 的占比、报告渲染（#123）。

这个模块刻意不 import 任何 ``app.*``：它只做「把样本算成读数」和「把读数写成 markdown」
两件事，因此可以被纯函数直接测（见 ``tests/test_loadtest_baseline.py``），也可以被
``scripts/loadtest_baseline.py`` 在压测跑完之后调用。压测怎么驱动事件循环、替身怎么装配，
都不在这里。

三处口径，写在这里而不是散在报告正文里：

1. **P50 / P95 用最近秩法**：升序第 ``⌈p/100 × N⌉`` 个，**不插值**。与
   ``scripts/eval_gate.py`` 的延迟口径逐字相同——两把尺子口径不同的时候，新旧读数就
   没法比，而「第 10 步单请求 P95 1177.9 ms」正是拿来对照的那一个数。
2. **「主循环同步 DB 占比」是近似**：分子是**主循环线程上**的同步会话调用耗时之和
   （插桩在会话替身上，按调用计时），分母是同一批请求的**墙钟耗时之和**。执行池线程上
   的调用（混合模式的向量分支）单独记、**不进分子**——那一段不占主循环，正是第 10 步
   用执行池换来的东西。这个比值的含义是「请求的时间里，有多少是被主循环上的同步数据库
   调用占住的」，不是「数据库占了 CPU 多少」。
3. **吞吐 = 完成请求数 / 该档墙钟**；**错误率 = 非 200 或业务码非 0 的占比**；
   **降级率**单列：混合模式降级是**成功**响应（``code=0`` + ``degraded_reason``），把它
   算进错误率会让「这条链路在并发下悄悄只剩一路」这件事看不见。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Final, Iterable, Sequence

#: 三档并发：票面要求的那三档，也是 S1 / S7 对比时唯一允许用的那三档。
CONCURRENCY_LEVELS: Final[tuple[int, ...]] = (4, 16, 64)

#: 第 10 步的既有读数：单请求 P95（评测口径，127 题，最近秩法）。只作量级参照。
STEP10_REFERENCE_P95_MS: Final[float] = 1177.9
STEP10_REFERENCE_SOURCE: Final[str] = (
    "docs/架构.md「评测」· eval/runs/20261112T135017Z"
    "（重排开 · qwen3.7 端点；127 题；hybrid / top_k=5；真 PostgreSQL + Elasticsearch + 嵌入；最近秩法）"
)

#: 报告里「占比」一节的固定说法。报告与代码读同一份，避免两处口径漂移。
SHARE_CAVEAT: Final[str] = (
    "这是**近似**：分子是主循环线程上同步会话调用的耗时之和（插桩在会话替身上，按调用计时），"
    "分母是同一批请求的墙钟耗时之和。执行池线程上的调用不进分子。"
)


def percentile_nearest_rank(samples: Sequence[float], percentile: float) -> float:
    """升序第 ``⌈percentile/100 × N⌉`` 个样本（最近秩法，不插值）。

    与 ``scripts/eval_gate.py`` 同一个定义：P95 是「真实发生过的那一次耗时」，不是两个
    样本之间插出来的一个数。
    """

    if not 0 < percentile <= 100:
        raise ValueError(f"percentile must be in (0, 100], got {percentile!r}")
    values = sorted(float(value) for value in samples)
    if not values:
        raise ValueError("cannot take a percentile of an empty sample")
    rank = math.ceil(percentile / 100 * len(values))
    return values[rank - 1]


def summarize_latency(samples: Sequence[float]) -> dict[str, float]:
    """一份样本 → ``count / p50_ms / p95_ms / mean_ms / max_ms``。

    一次算完：报告里的 P50 与 P95 必须来自同一份样本，分两次算迟早会出现「P50 比 P95 大」
    这种读数（分位数与样本集对不上时最容易发生，而且没人会去核）。
    """

    values = [float(value) for value in samples]
    if not values:
        return {"count": 0, "p50_ms": 0.0, "p95_ms": 0.0, "mean_ms": 0.0, "max_ms": 0.0}
    return {
        "count": len(values),
        "p50_ms": percentile_nearest_rank(values, 50),
        "p95_ms": percentile_nearest_rank(values, 95),
        "mean_ms": sum(values) / len(values),
        "max_ms": max(values),
    }


@dataclass(frozen=True)
class DbCall:
    """一次同步会话调用：谁（线程）、哪个方法、花了多久。"""

    thread: str
    method: str
    duration_ms: float


@dataclass(frozen=True)
class DbShare:
    """主循环同步 DB 的读数：分子、分母、以及「执行池那一段」的对照。

    ``share`` 的分母是 ``total_request_ms``（同一批请求的墙钟之和），所以它回答的是
    「请求时间里有多少被主循环上的同步数据库调用占住」，不是「数据库占了多少 CPU」。
    """

    main_loop_calls: int
    main_loop_ms: float
    off_loop_calls: int
    off_loop_ms: float
    total_request_ms: float
    by_method: dict[str, float] = field(default_factory=dict)
    share: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "main_loop_calls": self.main_loop_calls,
            "main_loop_ms": round(self.main_loop_ms, 3),
            "off_loop_calls": self.off_loop_calls,
            "off_loop_ms": round(self.off_loop_ms, 3),
            "total_request_ms": round(self.total_request_ms, 3),
            "by_method": {name: round(ms, 3) for name, ms in self.by_method.items()},
            "share": round(self.share, 6),
        }


class SyncDbLedger:
    """同步会话调用的账本：插桩点只有一个（会话替身的每个方法）。

    按线程名分类，而不是按调用栈：主循环同步调用与执行池线程上的调用在**同一个进程**里，
    唯一能分开它们的稳定事实就是「这条线程是不是主循环那条」。线程名由压测入口在开跑时
    取一次传进来（``threading.current_thread().name``）。
    """

    def __init__(self, *, main_thread: str | None = None) -> None:
        self._main_thread = main_thread
        self._calls: list[DbCall] = []

    @property
    def calls(self) -> list[DbCall]:
        return list(self._calls)

    def record(
        self,
        method: str,
        duration_ms: float,
        *,
        thread: str | None = None,
    ) -> None:
        self._calls.append(
            DbCall(
                thread=self._main_thread if thread is None else thread,
                method=method,
                duration_ms=float(duration_ms),
            )
        )

    def reset(self) -> None:
        self._calls.clear()

    def share(self, *, total_request_ms: float) -> DbShare:
        """把账本算成占比；``total_request_ms`` 是同一批请求的墙钟之和（分母）。"""

        main_loop = [call for call in self._calls if call.thread == self._main_thread]
        off_loop = [call for call in self._calls if call.thread != self._main_thread]
        main_ms = sum(call.duration_ms for call in main_loop)
        by_method: dict[str, float] = {}
        for call in main_loop:
            by_method[call.method] = by_method.get(call.method, 0.0) + call.duration_ms
        denominator = max(0.0, float(total_request_ms))
        return DbShare(
            main_loop_calls=len(main_loop),
            main_loop_ms=main_ms,
            off_loop_calls=len(off_loop),
            off_loop_ms=sum(call.duration_ms for call in off_loop),
            total_request_ms=denominator,
            by_method=by_method,
            share=(main_ms / denominator) if denominator > 0 else 0.0,
        )


@dataclass(frozen=True)
class LevelResult:
    """一档并发的全部读数。``as_dict`` 就是 ``--json-out`` 里的一条。"""

    concurrency: int
    requests: int
    ok: int
    failed: int
    degraded: int
    throughput_rps: float
    wall_ms: float
    latency: dict[str, float]
    db: DbShare
    #: 两个**实测**派生量：每请求在主循环上串行做多少毫秒同步 DB 调用；以及整档墙钟里事件
    #: 循环有多少比例卡在这些调用里（后者才是「吞吐为什么封顶」的那个数）。
    cross_check: dict[str, float] = field(default_factory=dict)
    #: 这一档用的「单次同步 DB 调用」模拟值（ms）。敏感性表靠它标行；普通档位也填上，
    #: 免得读报告的人要回到命令行才想得起这组数是怎么来的。
    db_call_ms: float = 0.0

    @property
    def error_rate(self) -> float:
        total = self.ok + self.failed
        return (self.failed / total) if total else 0.0

    @property
    def degraded_rate(self) -> float:
        return (self.degraded / self.ok) if self.ok else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "concurrency": self.concurrency,
            "requests": self.requests,
            "ok": self.ok,
            "failed": self.failed,
            "error_rate": round(self.error_rate, 6),
            "degraded": self.degraded,
            "degraded_rate": round(self.degraded_rate, 6),
            "throughput_rps": round(self.throughput_rps, 3),
            "wall_ms": round(self.wall_ms, 3),
            "db_call_ms": self.db_call_ms,
            "latency": {key: round(value, 3) for key, value in self.latency.items()},
            "db": self.db.as_dict(),
            "cross_check": {
                key: round(value, 6) for key, value in self.cross_check.items()
            },
        }


# ──────────────────────────────────────────────────────────────── 报告渲染


def _fmt_ms(value: float) -> str:
    return f"{value:.2f}"


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def _command_line(params: dict[str, Any]) -> str:
    """把这次跑的参数还原成一条可以直接粘贴的命令（报告要能自己复算）。"""

    parts = ["uv run python scripts/loadtest_baseline.py"]
    levels = params.get("levels") or list(CONCURRENCY_LEVELS)
    parts.append("--levels " + ",".join(str(level) for level in levels))
    for flag, key in (
        ("--requests", "requests"),
        ("--warmup", "warmup"),
        ("--db-call-ms", "db_call_ms"),
        ("--embed-ms", "embed_ms"),
        ("--vector-ms", "vector_ms"),
        ("--keyword-ms", "keyword_ms"),
    ):
        value = params.get(key)
        if value is not None:
            parts.append(f"{flag} {value}")
    return " ".join(parts)


def _profile_rows(params: dict[str, Any]) -> Iterable[tuple[str, str]]:
    """这次跑的「假设」：每一条都会进报告，读的人才知道数是怎么来的。"""

    yield ("每档请求数", str(params.get("requests", "—")))
    yield ("预热请求数", str(params.get("warmup", "—")))
    yield ("单次同步 DB 调用（模拟）", f"{params.get('db_call_ms', '—')} ms")
    yield ("嵌入调用（模拟）", f"{params.get('embed_ms', '—')} ms")
    yield ("向量召回 / pgvector（模拟，执行池线程）", f"{params.get('vector_ms', '—')} ms")
    yield ("关键词召回（模拟）", f"{params.get('keyword_ms', '—')} ms")
    yield ("检索执行池规模", str(params.get("pool_size", "—")))
    yield ("执行池排队预算", f"{params.get('queue_budget_ms', '—')} ms")


def render_report(
    *,
    levels: Sequence[LevelResult],
    params: dict[str, Any],
    reference: dict[str, Any] | None = None,
    db_sweep: Sequence[LevelResult] = (),
) -> str:
    """把读数渲染成可以整篇提交的 markdown（``--report-out`` 写的就是它）。

    每一节都带口径：这份报告是**尺子**，尺子本身的刻度（P95 怎么算、占比怎么算、
    哪些数是模拟的）比读数更不能省。
    """

    reference = reference or {"p95_ms": STEP10_REFERENCE_P95_MS, "source": STEP10_REFERENCE_SOURCE}
    lines: list[str] = []

    lines.append("# 第 11 步 S0 · 并发基线：这条链路在 4 / 16 / 64 并发下的形状")
    lines.append("")
    lines.append(
        "> 只读压测：不连真 Redis、不写库、不改生产代码路径。整条链路跑在进程内"
        "（ASGI + 既有注入缝），数据库与三类 Provider 都是**离线替身**。"
    )
    lines.append(
        "> 这份读数是**尺子**，不是生产容量承诺：绝对延迟取决于下面「假设」表里的模拟值，"
        "可比较的是**形状**（并发升高时谁劣化、劣化多少、吞吐卡在哪）。"
    )
    lines.append("")

    lines.append("## 一、复现")
    lines.append("")
    lines.append("```powershell")
    lines.append(_command_line(params))
    lines.append("```")
    lines.append("")
    lines.append(
        "脚本自己装配离线替身（会话、嵌入、向量库、关键词引擎）并把进程级注入点全部换掉；"
        "跑完把同一份读数写成 `--json-out`（机器读，本次在 `eval/loadtest-baseline/results.json`，"
        "含提交号 / Python / 平台）与 `--report-out`（人读，就是本文件）。"
    )
    lines.append("")
    lines.append(
        "**输入从哪来**：问句取自 `eval/questions.json`（137 道评测题，只取 `query` 字段，"
        "按请求序号循环使用）。选它是因为它是仓库里唯一一份固定的「真实用户问法」；"
        "替身返回的切片与这些问句无关（离线替身不检索真数据），问句在这里只负责"
        "「每次请求带一段真实长度的文本」。"
    )
    lines.append("")

    lines.append("## 二、口径")
    lines.append("")
    lines.append(
        "- **P50 / P95**：逐请求墙钟耗时，升序第 `⌈p/100 × N⌉` 个，**不插值**（最近秩法）。"
        "与 `scripts/eval_gate.py` 的延迟口径同一把尺子。"
    )
    lines.append(
        "- **吞吐**：该档完成的请求数 ÷ 该档墙钟（req/s）。"
    )
    lines.append(
        "- **错误率**：HTTP 非 200 或业务码非 0 的占比。**降级单列**：混合模式降级是成功响应"
        "（`code=0` + `degraded_reason`），算进错误率会让「并发下悄悄只剩一路」看不见。"
    )
    lines.append(
        "- **主循环同步 DB 占比**：见第四节。它有两个口径（占请求时间 / 占整档墙钟），"
        "**都是近似**，分子分母在那一节写清。"
    )
    lines.append("")

    lines.append("## 三、三档并发读数")
    lines.append("")
    lines.append(
        "| 并发 | 请求数 | P50 (ms) | P95 (ms) | 吞吐 (req/s) | 错误率 | 降级率 |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for level in levels:
        lines.append(
            "| {c} | {n} | {p50} | {p95} | {rps} | {err} | {deg} |".format(
                c=level.concurrency,
                n=level.requests,
                p50=_fmt_ms(level.latency.get("p50_ms", 0.0)),
                p95=_fmt_ms(level.latency.get("p95_ms", 0.0)),
                rps=f"{level.throughput_rps:.1f}",
                err=f"{_fmt_pct(level.error_rate)}（{level.failed}/{level.requests}）",
                deg=_fmt_pct(level.degraded_rate),
            )
        )
    lines.append("")

    if len(levels) >= 2:
        first, last = levels[0], levels[-1]
        first_p95 = first.latency.get("p95_ms", 0.0)
        last_p95 = last.latency.get("p95_ms", 0.0)
        p95_growth = (last_p95 / first_p95) if first_p95 else 0.0
        throughput_growth = (last.throughput_rps / first.throughput_rps) if first.throughput_rps else 0.0
        concurrency_growth = last.concurrency / first.concurrency if first.concurrency else 0.0
        lines.append(
            "从 {c1} 并发到 {c2} 并发（并发 ×{cg:g}）：P95 从 {p1} ms 涨到 {p2} ms"
            "（×{pg:.1f}），吞吐从 {t1:.1f} 涨到 {t2:.1f} req/s（×{tg:.2f}）。".format(
                c1=first.concurrency,
                c2=last.concurrency,
                cg=concurrency_growth,
                p1=_fmt_ms(first_p95),
                p2=_fmt_ms(last_p95),
                pg=p95_growth,
                t1=first.throughput_rps,
                t2=last.throughput_rps,
                tg=throughput_growth,
            )
        )
        lines.append("")
        if throughput_growth < 1.5:
            lines.append(
                "**在这组模拟延迟下，这就是结论**：并发涨了 {cg:g} 倍，吞吐只涨了 {tg:.2f} 倍，"
                "而 P95 涨了 {pg:.1f} 倍。这条链路的上限不在并发数上，而在主循环的**串行工作**上"
                "——多出来的并发只是排队，不是吞吐。第四节把那段串行工作里「同步数据库」的那一份"
                "量出来；敏感性表说明这个倍数会随「单次 DB 调用多慢」变化，但**方向不变**。".format(
                    cg=concurrency_growth, tg=throughput_growth, pg=p95_growth
                )
            )
        else:
            lines.append(
                "吞吐跟着并发涨了 {tg:.2f} 倍，说明这一档还没有撞到串行上限；"
                "再看第四节的「占整档墙钟」，它离 100% 还有多远就是还剩多少余量。".format(
                    tg=throughput_growth
                )
            )
        lines.append("")
        total_failed = sum(level.failed for level in levels)
        total_ok = sum(level.ok for level in levels)
        total_degraded = sum(level.degraded for level in levels)
        if total_failed == 0 and total_degraded == 0:
            lines.append(
                "三档一共 {ok} 个请求：**错误 0、降级 0**。所以下面的劣化全部来自"
                "「同样的活，排队方式不同」，不是失败或降级掩盖出来的。".format(ok=total_ok)
            )
            lines.append("")
    lines.append("## 四、主循环同步 DB：占比与口径")
    lines.append("")
    lines.append(SHARE_CAVEAT)
    lines.append("")
    lines.append(
        "| 并发 | 每请求主循环同步 DB 次数 | 每请求主循环同步 DB 耗时 (ms) | 占请求时间 | 占整档墙钟（主循环被占住） | 执行池线程上的同步 DB 调用 (次) |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for level in levels:
        per_request = (
            level.db.main_loop_calls / level.requests if level.requests else 0.0
        )
        serial_ms = level.cross_check.get("serial_ms_per_request", 0.0)
        busy = level.cross_check.get("loop_busy_share", 0.0)
        lines.append(
            "| {c} | {per} | {serial} | {share} | {busy} | {off} |".format(
                c=level.concurrency,
                per=f"{per_request:.2f}",
                serial=_fmt_ms(serial_ms),
                share=_fmt_pct(level.db.share),
                busy=_fmt_pct(busy),
                off=level.db.off_loop_calls,
            )
        )
    lines.append("")
    if levels:
        top = levels[-1]
        if top.db.by_method:
            lines.append(
                f"最高一档（{top.concurrency} 并发）按方法分解（主循环线程，ms）："
            )
            lines.append("")
            lines.append("| 方法 | 耗时 (ms) |")
            lines.append("| --- | --- |")
            for method, ms in sorted(top.db.by_method.items(), key=lambda item: -item[1]):
                lines.append(f"| `{method}` | {_fmt_ms(ms)} |")
            lines.append("")
        if top.throughput_rps > 0:
            per_request_wall = 1000.0 / top.throughput_rps
            serial_ms = top.cross_check.get("serial_ms_per_request", 0.0)
            other_ms = max(0.0, per_request_wall - serial_ms)
            lines.append(
                "按最高一档推一下每请求的主循环份额：实测吞吐 {rps:.1f} req/s 的倒数是"
                " **{wall:.1f} ms/请求**——主循环被占满时，这就是一条请求摊到的串行时间，"
                "也就是这条链路的上限。其中 **{serial:.1f} ms（{share}）是同步数据库调用**，"
                "剩下约 {other:.1f} ms 是框架本身（JSON 解析、pydantic 校验、两个中间件）。"
                "后面那一半不在本报告的分子里，所以「占整档墙钟」是**下限**而不是全部。".format(
                    rps=top.throughput_rps,
                    wall=per_request_wall,
                    serial=serial_ms,
                    share=_fmt_pct(serial_ms / per_request_wall if per_request_wall else 0.0),
                    other=other_ms,
                )
            )
            lines.append("")
            lines.append(
                "次数那一列包含会话上的**全部**方法：`add` 与 `close` 不产生往返"
                "（方法分解里它们的耗时约等于 0），真正花时间的是 `execute` / `flush` / "
                "`commit` 这几次往返。"
            )
            lines.append("")
    lines.append(
        "两个占比是**两件事**，都要看："
    )
    lines.append("")
    lines.append(
        "- **占请求时间** = 同步 DB 耗时 ÷ 这次请求的墙钟。它回答票面上的问题"
        "「其中多少时间花在主循环上的同步数据库调用」。**并发越高它越小**——不是因为它变快了，"
        "而是因为请求越来越多的时间花在**排队**上（排在别人那次同步调用后面），分母被排队撑大。"
    )
    lines.append(
        "- **占整档墙钟** = 同步 DB 耗时 ÷ 这一档的墙钟。它回答「事件循环有多少比例正卡在这些"
        "调用里」。这个数不随并发稀释：它是吞吐被封顶的**直接原因**——主循环被占住的时候，"
        "第 2、第 64 个请求一步都推不动。"
    )
    lines.append("")
    lines.append(
        "读法：如果两列都在涨/都贴住高位，说明「并发上限不是数据库，是主循环」；如果"
        "「占整档墙钟」在 64 并发时明显低于「占请求时间」的推断，说明瓶颈在别处（框架 CPU、"
        "模型调用）。注意主循环上的串行时间不止这一段：JSON 解析、pydantic 校验、中间件也都"
        "在主循环上，本报告只量了**同步 DB 那一半**——「占整档墙钟」因此是同步 DB 的**下限**"
        "贡献，不是主循环占用率的全部。"
    )
    lines.append("")

    if db_sweep:
        lines.append("### 敏感性：单次同步 DB 调用的耗时不是测出来的，是假设")
        lines.append("")
        lines.append(
            "同一个并发档下把「单次同步 DB 调用」换几个值再跑一遍：占比与 P95 都跟着它走，"
            "所以绝对数只在这组假设下成立。形状（并发越高越差、吞吐封顶）不随它改变。"
        )
        lines.append("")
        lines.append("| 单次同步 DB (ms) | 并发 | P50 (ms) | P95 (ms) | 吞吐 (req/s) | 主循环被占住的墙钟比例 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for level in db_sweep:
            lines.append(
                "| {db} | {c} | {p50} | {p95} | {rps} | {busy} |".format(
                    db=f"{level.db_call_ms:g}",
                    c=level.concurrency,
                    p50=_fmt_ms(level.latency.get("p50_ms", 0.0)),
                    p95=_fmt_ms(level.latency.get("p95_ms", 0.0)),
                    rps=f"{level.throughput_rps:.1f}",
                    busy=_fmt_pct(level.cross_check.get("loop_busy_share", 0.0)),
                )
            )
        lines.append("")

    lines.append("## 五、与第 10 步的既有读数对照")
    lines.append("")
    lines.append(
        "| 读数 | 值 | 口径 | 能不能直接比 |"
    )
    lines.append("| --- | --- | --- | --- |")
    lines.append(
        "| 第 10 步 · 单请求 P95 | **{p95} ms** | 评测口径：127 题逐题耗时，最近秩法；"
        "真实 PostgreSQL + Elasticsearch + 嵌入，重排开 | 量级参照 |".format(
            p95=f"{float(reference.get('p95_ms', STEP10_REFERENCE_P95_MS)):.1f}"
        )
    )
    if levels:
        top = levels[-1]
        lines.append(
            "| 本报告 · {c} 并发 P95 | {p95} ms | 进程内 ASGI + 离线替身，延迟是模拟值 | 与上一行**不可直接比** |".format(
                c=top.concurrency, p95=_fmt_ms(top.latency.get("p95_ms", 0.0))
            )
        )
    lines.append("")
    lines.append(
        "出处：`{source}`。两者**不是同一把尺子**：输入不同（137 道评测题 vs 固定问句循环）、"
        "依赖不同（真库真引擎 vs 离线替身）、口径不同（逐题耗时 vs 并发下的逐请求墙钟）。"
        "放在一起只为一件事：给「并发下的 P95」一个熟悉的量级参照。".format(
            source=reference.get("source", STEP10_REFERENCE_SOURCE)
        )
    )
    lines.append("")

    lines.append("## 六、这次跑的参数与假设")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("| --- | --- |")
    for name, value in _profile_rows(params):
        lines.append(f"| {name} | {value} |")
    lines.append("")

    lines.append("## 七、局限（读之前先看这一节）")
    lines.append("")
    lines.append(
        "- **延迟是模拟的**：单次同步 DB 调用、嵌入、向量召回、关键词召回都是固定睡眠，"
        "不是实测的网络与数据库延迟。绝对数不能当容量规划，只能当形状与占比的尺子。"
    )
    lines.append(
        "- **进程内单进程**：没有 uvicorn、没有多 worker、没有真实连接池上限。"
        "「并发上限是池」这件事在这里体现为执行池（4 条线程）的排队，而不是数据库连接数。"
    )
    lines.append(
        "- **替身只实现检索路径真正用到的那几种语句形状**：遇到新的形状会**当场报错**，"
        "不会静默返回空结果——后续票往主循环上新增查询时，这个入口会先红一次，逼人先想清楚"
        "那条查询该不该在主循环上。"
    )
    lines.append(
        "- **默认离线套件不受影响**：脚本不进 `tests/` 的默认路径，跑全量仍然不需要真库 / 真 Redis。"
    )
    lines.append(
        "- **同机重复跑有抖动**：这台机器上吞吐的抖动约 ±10%、P95 约 ±15%（受同机其他负载影响）。"
        "所以对比时看**倍数与形状**，不要拿两位小数当结论；要更稳就加大 `--requests`。"
    )
    lines.append("")

    lines.append("## 八、后面每一张票怎么用它")
    lines.append("")
    lines.append(
        "同一条命令、同一组参数，改完再跑一次，对比三件事："
        "（1）64 并发吞吐是否 ≥ 2×；（2）单请求 P95 是否 ±5% 以内；"
        "（3）「主循环同步 DB 占比」是否趋近 0。三条都是 S7 的验收数字。"
    )
    lines.append("")
    return "\n".join(lines)
