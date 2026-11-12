"""统一评测运行器：一条命令完成「校验 → 跑 → 打分 → 出报告 → 存档」。

四个子命令，对应票面上的四件事：

``validate-dataset``
    离线校验题库与标注：题型齐不齐、``absent`` 有没有乱写标注、每个
    ``(document, heading_path)`` 是否真能在**当前切块口径**下定位到切片。不连库、不联网。

``validate-config``
    校验实验配置（``eval/experiment.toml``）：键名、取值、两条判据不能自相矛盾。

``run``
    真实跑一次：**进程内**调 ``app.services.rag_service.retrieve()``（不经 HTTP），
    逐题拿 ``retrieved_chunk_ids``，用 ragas 的 **ID 版** ``context_precision`` /
    ``context_recall`` 打分（零 LLM），与一份已归档的基线比，产出
    ``eval/runs/<UTC 时间戳>/`` 下的 ``report.md`` + ``results.json`` + ``environment.json``。
    可选 ``--via-http <base_url>`` 另跑一条 HTTP 冒烟（3 道题、只断言字段齐全，不进闸门）。

``report``
    从归档重出一份报告（只读，不再跑检索）；``--select <键>`` 单独打印一个数字——
    报告里每个数字旁边都写着这条命令，这就是「每个数字都附复现命令」的落点。

为什么默认走进程内而不是 HTTP：门禁要能回答「这次改动把检索弄好还是弄坏了」。走 HTTP
就多了一层序列化、鉴权、档位门禁与配额，任何一层变了都会让数字动，而你不知道是不是
检索本身变了。HTTP 那条留作冒烟，专门抓「进进程内对、序列化之后丢字段」这类问题。

**没有 .env、没有网络也要能 import 本模块**：``app.*`` 与 ``scripts.eval_retrieval``
全部惰性导入（在子命令里），模块顶层只碰标准库与本仓的纯函数
（``scripts/eval_gate.py``）。否则 ``pytest`` 一收集就崩，离线单测无从谈起。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    # 以 ``python scripts/eval_runner.py`` 方式运行时 sys.path[0] 是 scripts/，
    # 仓库根不在里面；``scripts`` 是没有 __init__.py 的命名空间包。
    sys.path.insert(0, str(REPO_ROOT))

from scripts.eval_gate import (  # noqa: E402 - 必须在 sys.path 补好之后
    DEFAULT_OVERALL_PP,
    DEFAULT_PER_TYPE_PP,
    EXCLUDED_QUESTION_TYPES,
    GateThresholds,
    QuestionScore,
    build_results_payload,
    compare_to_baseline,
    context_scores,
    corpus_version,
    render_report,
    score_run,
    select_value,
    validate_dataset,
    validate_experiment,
)

EVAL_DIR = REPO_ROOT / "eval"
CORPUS_DIR = EVAL_DIR / "corpus"
QUESTIONS_PATH = EVAL_DIR / "questions.json"
MANIFEST_PATH = CORPUS_DIR / "manifest.json"
EXPERIMENT_PATH = EVAL_DIR / "experiment.toml"
RUNS_DIR = EVAL_DIR / "runs"
#: 随仓库走的基线（``run --update-baseline`` 会写这两份）。
#: 归档的 run 目录在 ``.gitignore`` 里（它们是本机产物），基线要能进仓库——
#: 否则换一台机器 / 换一个 clone，闸门就没有比较对象。
BASELINE_DIR = EVAL_DIR / "baseline"

RUN_SCHEMA = "eval-run/1"

#: 退出码：0 通过 / 1 门禁红（或校验不通过）/ 2 跑不起来。
EXIT_OK = 0
EXIT_RED = 1
EXIT_FAILED = 2

#: HTTP 冒烟的响应契约：这些字段缺任何一个都说明「序列化这一层丢了东西」。
REQUIRED_DATA_FIELDS = ("query", "kb_ids", "retrieved_chunks", "metadata")
REQUIRED_METADATA_FIELDS = ("mode", "top_k", "vector_store", "retrieval", "trace_id")
REQUIRED_RETRIEVAL_FIELDS = ("degraded", "degraded_reason", "empty_reason", "rerank")
REQUIRED_CHUNK_FIELDS = ("chunk_id", "kb_id", "score")

DEFAULT_HTTP_ENDPOINT = "/api/v1/rag/retrieve"


# ─────────────────────────────────────────────────────── 小工具（纯函数，可离线测）


def utc_stamp(moment: datetime | None = None) -> str:
    """run id：UTC 时间戳，秒级，形如 ``20261111T062000Z``。

    用 UTC 而不是本地时间：报告会被贴进工单、被跨时区的人读，本地时间少一个时区就没法对。
    """

    moment = moment or datetime.now(UTC)
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def json_safe(value: Any) -> Any:
    """把 ``NaN`` / ``Infinity`` 换成 ``None``，其余原样。

    ``json.dumps`` 默认会写出 ``NaN``——那不是合法 JSON，下一次「与基线比」读回来就炸，
    而人读的那份 report.md 早已生成好，谁也不会注意到。所以归档前统一洗一遍，
    并且用 ``allow_nan=False`` 兜一层：洗漏了就让写文件当场失败，而不是写出坏文件。
    """

    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def _redact_database_url(url: str) -> str:
    """去掉连接串里的口令：环境快照会进仓库，口令不该跟着进去。"""

    match = re.match(
        r"^(?P<scheme>[^:/]+(?:\+[^:]+)?://)(?P<user>[^:/@]*)(?P<secret>:[^@]*)?@(?P<rest>.*)$", url
    )
    if not match:
        return url
    secret = ":***" if match.group("secret") else ""
    return f"{match.group('scheme')}{match.group('user')}{secret}@{match.group('rest')}"


def select_smoke_questions(questions: Sequence[Any], samples: int) -> list[Any]:
    """HTTP 冒烟挑题：先每个题型各取一道（题型覆盖），不够再按原顺序补齐。

    固定顺序 = 固定样本：冒烟报告要能两次对比，随机挑题会让「这次为什么不一样」问不清。
    """

    picked: list[Any] = []
    seen_types: set[str] = set()
    for question in questions:
        if question.type in seen_types:
            continue
        seen_types.add(question.type)
        picked.append(question)
        if len(picked) >= samples:
            return picked
    for question in questions:
        if question in picked:
            continue
        picked.append(question)
        if len(picked) >= samples:
            break
    return picked


def missing_response_fields(body: Any) -> list[str]:
    """HTTP 响应的字段完整性：返回「缺了什么」，空列表表示齐全。

    只看**契约里写过**的字段（``data`` / ``metadata`` / ``retrieval`` 三层的固定键），
    不看值的业务含义——这是冒烟，不是门禁。
    """

    if not isinstance(body, Mapping):
        return ["响应体不是 JSON 对象"]
    missing: list[str] = []
    if body.get("code") != 0:
        missing.append(f"code={body.get('code')!r}（应为 0）")
    data = body.get("data")
    if not isinstance(data, Mapping):
        return missing + ["data 缺失或不是对象"]
    for key in REQUIRED_DATA_FIELDS:
        if key not in data:
            missing.append(f"data.{key}")
    metadata = data.get("metadata")
    if isinstance(metadata, Mapping):
        for key in REQUIRED_METADATA_FIELDS:
            if key not in metadata:
                missing.append(f"metadata.{key}")
        retrieval = metadata.get("retrieval")
        if isinstance(retrieval, Mapping):
            for key in REQUIRED_RETRIEVAL_FIELDS:
                if key not in retrieval:
                    missing.append(f"metadata.retrieval.{key}")
        elif "metadata.retrieval" not in missing:
            missing.append("metadata.retrieval（不是对象）")
    chunks = data.get("retrieved_chunks")
    if isinstance(chunks, list) and chunks:
        first = chunks[0]
        if isinstance(first, Mapping):
            for key in REQUIRED_CHUNK_FIELDS:
                if key not in first:
                    missing.append(f"data.retrieved_chunks[0].{key}")
    return missing


# ───────────────────────────────────────────────────────── 环境变量与配置


def preload_env_file(path: str | None) -> str | None:
    """把 ``--env-file`` 灌进 ``os.environ``，**必须**早于任何 ``app.*`` 导入。

    ``settings`` 是模块级单例（``app/core/config.py`` 末尾 ``settings = Settings()``），
    导入即定型；所以这条只能在导入之前做。``setdefault`` 让显式环境变量优先于文件，
    与 pydantic-settings 的优先级一致。

    实现收在 :func:`scripts.env_bootstrap.preload_env_file_from`——本文件原先自己抄了一份
    ``dotenv_values`` + ``os.environ.setdefault``，与本仓另外三个脚本同一套逻辑。
    """

    from scripts.env_bootstrap import preload_env_file_from

    return preload_env_file_from(path)


def load_experiment(path: Path) -> dict[str, Any]:
    """读实验配置（TOML）。缺文件 / 写坏都是**使用者错误**，当场说清哪个文件。"""

    if not path.is_file():
        raise SystemExit(f"实验配置不存在：{path}")
    try:
        return _toml_load(path)
    except Exception as exc:  # noqa: BLE001 - TOML 解析失败的信息足够定位
        raise SystemExit(f"实验配置解析失败（{path}）：{exc}") from exc


def _toml_load(path: Path) -> dict[str, Any]:
    import tomllib

    with path.open("rb") as handle:
        return tomllib.load(handle)


# ─────────────────────────────────────────────────────── 语料快照与标注定位


def corpus_snapshot(
    *,
    corpus_dir: Path = CORPUS_DIR,
    manifest_path: Path = MANIFEST_PATH,
    chunk_size: int,
    chunk_overlap: int,
    table_max_rows: int,
) -> dict[str, Any]:
    """用**真实切块器**算出「(文档, heading_path) → 片数」，并给出语料的基本事实。

    这是 ``--reuse`` 与否都不变的一步：它回答「标注在这份语料上还对不对」，与数据库里
    有没有切片无关。与 ``scripts/eval_retrieval.py`` 的标签解析同一条规则（0 片即失败），
    只是这里不需要连库。
    """

    from app.utils.markdown_splitter import MarkdownStructuredSplitter

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    splitter = MarkdownStructuredSplitter(
        chunk_size=chunk_size, chunk_overlap=chunk_overlap, table_max_rows=table_max_rows
    )
    files: dict[str, str] = {}
    locators: dict[tuple[str, str], int] = {}
    documents = 0
    chunks = 0
    kb_ids: list[str] = []
    for kb in manifest["kbs"]:
        kb_ids.append(str(kb["kb_id"]))
        for filename in kb["documents"]:
            path = corpus_dir / filename
            if not path.is_file():
                raise SystemExit(f"manifest 声明了 {filename}，但 {path} 不存在")
            text = path.read_text(encoding="utf-8")
            files[filename] = text
            documents += 1
            for piece in splitter.split(text):
                chunks += 1
                key = (path.stem, str(piece.metadata["heading_path"]))
                locators[key] = locators.get(key, 0) + 1
    return {
        "files": files,
        "locators": locators,
        "documents": documents,
        "chunks": chunks,
        "kb_ids": kb_ids,
    }


# ───────────────────────────────────────────────────────────── 环境快照


def _git(*arguments: str) -> str | None:
    try:
        completed = subprocess.run(  # noqa: S603 - 固定参数，无 shell
            ["git", *arguments],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:  # noqa: BLE001 - 没有 git 也要能跑（离线 / 打包环境）
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def environment_snapshot(
    *,
    run_id: str,
    generated_at: str,
    experiment: Mapping[str, Any],
    corpus: Mapping[str, Any],
    dataset_version: str,
    env_file: str | None,
) -> dict[str, Any]:
    """环境快照：**这次数字是在什么条件下算出来的**。

    票面点名的五件事都在：检索模型（模式 / 开关 / 融合常数）、嵌入模型、语料版本与题数、
    开关状态、以及门禁指标用的是哪个实现（ragas 私有路径 + 版本）。另附运行时与 git
    提交——「同配置两跑一致」这句话，得先说得清「配置」指哪些行。
    """

    from importlib.metadata import version

    from app.core.config import settings

    try:
        ragas_version = version("ragas")
    except Exception:  # noqa: BLE001 - 缺元数据不该让评测跑不完
        ragas_version = None

    return {
        "run_id": run_id,
        "generated_at": generated_at,
        "schema": RUN_SCHEMA,
        "retrieval": {
            "mode": experiment.get("mode"),
            "top_k": experiment.get("top_k"),
            "fold_to_sections": True,
            "expand_table_rows": True,
            "rerank_enabled": bool(settings.RERANK_ENABLED),
            "query_rewrite_enabled": bool(settings.QUERY_REWRITE_ENABLED),
            "synonym_expansion_enabled": bool(settings.SYNONYM_EXPANSION_ENABLED),
            "vector_store": settings.VECTOR_STORE,
            "hybrid_recall_top_k": settings.HYBRID_RECALL_TOP_K,
            "fusion": "rrf",
        },
        "embedding": {
            "provider": settings.EMBEDDING_PROVIDER,
            "model": settings.EMBEDDING_MODEL,
            "dimensions": settings.EMBEDDING_DIMENSIONS,
        },
        "keyword": {
            "provider": settings.KEYWORD_SEARCH_PROVIDER,
            "url": settings.ELASTICSEARCH_URL,
            "index": settings.ELASTICSEARCH_INDEX,
        },
        "chunking": {
            "split_strategy": experiment.get("split_strategy"),
            "chunk_size": settings.CHUNK_SIZE,
            "chunk_overlap": settings.CHUNK_OVERLAP,
            "table_max_rows": settings.TABLE_MAX_ROWS_PER_CHUNK,
        },
        "gate_metrics": {
            # 私有路径 + 版本一起记：ragas 换实现时，「同一个名字」不再保证同一个数。
            "context_precision": "ragas.metrics._context_precision.IDBasedContextPrecision",
            "context_recall": "ragas.metrics._context_recall.IDBasedContextRecall",
            "ragas_version": ragas_version,
            "needs_llm": False,
            "needs_reference_answer": False,
        },
        "judge": {
            "model": None,
            "note": "LLM 裁判是旁证指标（只提示、不阻断），不在本票范围；见 #108 / #109。",
        },
        "corpus": dict(corpus),
        "dataset": {"questions_file": "eval/questions.json", "version": dataset_version},
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "git_commit": _git("rev-parse", "--short", "HEAD"),
            "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "git_dirty": bool(_git("status", "--porcelain")),
            "database": _redact_database_url(str(settings.DATABASE_URL)),
            "env_file": env_file,
        },
    }


# ───────────────────────────────────────────────────── 归档：写 / 读 run 目录


def _promote_baseline(
    payload: Mapping[str, Any], *, baseline_dir: Path | None = None
) -> dict[str, Any]:
    """把一次 run 变成本仓库的固定基线（缺省 ``eval/baseline/``，也可另存一份）。

    只改 ``run_dir`` 一处：基线副本里的复现命令必须指向**它自己**，否则换一台机器
    clone 下来，报告里那些 ``--select`` 命令全部指向一个不存在的 ``eval/runs/<id>``。
    原始 run 目录留在 ``promoted_from`` 里，档案链没断。
    """

    target = baseline_dir or BASELINE_DIR
    promoted = dict(payload)
    promoted["promoted_from"] = payload.get("run_dir")
    promoted["run_dir"] = _display_path(target)
    return promoted


def write_run_dir(run_dir: Path, payload: Mapping[str, Any]) -> list[Path]:
    """把一份 run 落成三个文件：人读报告 + 机读结果 + 环境快照。

    三份都要写：只有报告，下次对比得人肉抄数字；只有结果，人读不出「为什么红」；
    没有环境快照，「同配置」这句话就无从核对。
    """

    run_dir.mkdir(parents=True, exist_ok=True)
    results = json_safe(dict(payload))
    environment = json_safe(dict(payload.get("environment") or {}))
    written: list[Path] = []
    for name, content in (
        ("results.json", results),
        ("environment.json", environment),
    ):
        path = run_dir / name
        path.write_text(
            json.dumps(content, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        written.append(path)
    report_path = run_dir / "report.md"
    report_path.write_text(render_report(results), encoding="utf-8")
    written.append(report_path)
    return written


def read_run(run_dir: Path) -> dict[str, Any]:
    results_path = run_dir / "results.json"
    if not results_path.is_file():
        raise SystemExit(f"{run_dir} 里没有 results.json；--run 要指向一个归档目录")
    payload = json.loads(results_path.read_text(encoding="utf-8"))
    environment_path = run_dir / "environment.json"
    if environment_path.is_file():
        payload.setdefault("environment", json.loads(environment_path.read_text(encoding="utf-8")))
    return payload


def resolve_run_dir(reference: str) -> Path:
    """``--run`` 接受：归档目录、``results.json``、或裸 run id。"""

    candidate = Path(reference)
    if not candidate.is_absolute():
        candidate = REPO_ROOT / candidate
    if candidate.is_file():
        return candidate.parent
    if candidate.is_dir():
        return candidate
    if (RUNS_DIR / reference).is_dir():
        return RUNS_DIR / reference
    raise SystemExit(f"找不到 run：{reference}（试过目录、results.json 与 eval/runs/{reference}）")


# ─────────────────────────────────────────────────────────── 基线挑选


def resolve_baseline_dir(reference: str | None = None) -> Path:
    """``--baseline-dir`` → 目录；留空就是随仓库走的 ``eval/baseline/``。

    读与写走**同一个**参数：拿 A 当基线比、把结果写进 B，会让「下一次跟谁比」悄悄换人。
    另存第二份基线（例如重排开的那份 ``eval/baseline-rerank``）也靠它，两种口径各占一个
    目录，谁也不会覆盖谁。
    """

    if reference is None or not str(reference).strip():
        return BASELINE_DIR
    path = Path(str(reference).strip())
    return path if path.is_absolute() else REPO_ROOT / path


def _archived_rerank_enabled(payload: Mapping[str, Any]) -> bool:
    """归档里记录的「这一轮重排开没开」；老档案缺字段按「关」算（该字段自第一版就有）。"""

    retrieval = (payload.get("environment") or {}).get("retrieval") or {}
    return bool(retrieval.get("rerank_enabled"))


def _switch_text(enabled: bool) -> str:
    return "开" if enabled else "关"


def baseline_overwrite_problem(
    baseline_dir: Path, *, rerank_enabled: bool, force: bool = False
) -> str | None:
    """要不要拒绝这次覆盖：目标目录已有基线、且「重排开关」与本次不同 → 拒绝。

    返回拒绝理由，``None`` 表示放行。

    为什么单独一条护栏：``report --update-baseline`` 会**重写归档**。一次手滑就能把冻结
    基线（CP 0.2220 / CR 0.8717，重排关）换成一次重排开着跑的读数——数字看起来依然
    「合理」，没有报错，也没有 diff 会提醒你。所以宁可拦下来，让人显式二选一：
    ``--baseline-dir`` 另存一份，或者 ``--force`` 明说要覆盖。
    """

    if force:
        return None
    results_path = baseline_dir / "results.json"
    if not results_path.is_file():
        return None  # 还没有基线：第一次钉基线不受这条约束
    try:
        existing = json.loads(results_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None  # 读不出来就谈不上「已知开关不同」，不拿它拦人
    if not isinstance(existing, Mapping):
        return None
    previous = _archived_rerank_enabled(existing)
    if previous == bool(rerank_enabled):
        return None
    return (
        f"拒绝覆盖基线 {_display_path(baseline_dir)}：它是「{_switch_text(previous)}」重排跑的，"
        f"这次是「{_switch_text(bool(rerank_enabled))}」。"
        "换了尺子的读数不能盖在旧基线上——用 --baseline-dir 另存一份（例如 "
        "eval/baseline-rerank）；确认要覆盖就显式加 --force。"
    )


def _baseline_candidates() -> list[Path]:
    """``eval/runs/`` 下能当基线的 run，按 run id（= UTC 时间）从新到旧。

    只认**全量**且状态为 ``ok`` 的 run：一次 ``--limit 5`` 的冒烟不能被当成基线，
    否则下一次全量跑会拿 5 道题的数去比 127 道题。
    """

    if not RUNS_DIR.is_dir():
        return []
    candidates: list[Path] = []
    for run_dir in sorted(RUNS_DIR.iterdir(), reverse=True):
        if not run_dir.is_dir() or not (run_dir / "results.json").is_file():
            continue
        try:
            payload = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if payload.get("status") != "ok":
            continue
        if not (payload.get("scope") or {}).get("full"):
            continue
        candidates.append(run_dir)
    return candidates


def resolve_baseline(
    *,
    reference: str | None,
    current_run_id: str | None = None,
    baseline_dir: Path | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """挑一份基线，返回 ``(payload, 来源说明)``。

    顺序：显式指定（命令行 / 配置里的 ``[gate].baseline``）→ ``baseline_dir``（缺省
    ``eval/baseline/``，随仓库走的那份）→ ``eval/runs/`` 下最近一次全量归档 → 没有基线。

    ``reference`` 取 ``"none"`` 表示明确「本次不比」。
    """

    if reference and reference.strip().lower() in {"none", "off", "-"}:
        return None, None

    if reference and reference.strip():
        path = Path(reference.strip())
        if not path.is_absolute():
            path = REPO_ROOT / path
        if path.is_dir():
            run_dir = path
        elif path.is_file() and path.name == "results.json":
            run_dir = path.parent
        else:
            run_dir = resolve_run_dir(reference.strip())
        return read_run(run_dir), _display_path(run_dir / "results.json")

    pinned = baseline_dir or BASELINE_DIR
    baseline_results = pinned / "results.json"
    if baseline_results.is_file():
        return read_run(pinned), _display_path(baseline_results)

    for run_dir in _baseline_candidates():
        if current_run_id and run_dir.name == current_run_id:
            continue
        return read_run(run_dir), _display_path(run_dir / "results.json")
    return None, None


def _display_path(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


# ─────────────────────────────────────────────────────────────── 子命令：校验


def _chunking_settings() -> dict[str, int]:
    from app.core.config import settings

    return {
        "chunk_size": settings.CHUNK_SIZE,
        "chunk_overlap": settings.CHUNK_OVERLAP,
        "table_max_rows": settings.TABLE_MAX_ROWS_PER_CHUNK,
    }


def cmd_validate_dataset(args: argparse.Namespace) -> int:
    questions_path = Path(args.questions)
    if not questions_path.is_file():
        print(f"题库不存在：{questions_path}", file=sys.stderr)
        return EXIT_FAILED
    payload = json.loads(questions_path.read_text(encoding="utf-8"))
    snapshot = corpus_snapshot(
        corpus_dir=Path(args.corpus_dir),
        manifest_path=Path(args.manifest),
        **_chunking_settings(),
    )
    from app.services.retrieval_options import MAX_TOP_K

    report = validate_dataset(payload, locators=snapshot["locators"], top_k_max=MAX_TOP_K)
    stats = report["stats"]
    excluded_text = (
        "、".join(f"{name} {count}" for name, count in stats["excluded"].items()) or "无"
    )
    print("=== 题库校验（离线：不连库、不发网络）===")
    print(f"题库      : {questions_path}（k={payload.get('k')}）")
    print(
        f"题目      : {stats['questions']} 道（可回答 {stats['answerable']} 道，"
        f"进平均 {stats['gate']} 道；排除 {excluded_text}）"
    )
    print(
        f"语料      : {snapshot['documents']} 篇 / {snapshot['chunks']} 片；"
        f"标注 {stats['labels']} 个全部能在当前切块口径下定位"
    )
    for warning in report["warnings"]:
        print(f"WARN      : {warning}")
    for problem in report["problems"]:
        print(f"PROBLEM   : {problem}")
    if report["problems"]:
        print(f"\n结论：不通过（{len(report['problems'])} 处问题）", file=sys.stderr)
        return EXIT_RED
    print("\n结论：通过")
    return EXIT_OK


def cmd_validate_config(args: argparse.Namespace) -> int:
    from app.core.config import SUPPORTED_RETRIEVAL_MODES
    from app.models.knowledge_base import SPLIT_STRATEGIES
    from app.services.retrieval_options import MAX_TOP_K

    config = load_experiment(Path(args.config))
    report = validate_experiment(
        config,
        modes=SUPPORTED_RETRIEVAL_MODES,
        strategies=SPLIT_STRATEGIES,
        top_k_max=MAX_TOP_K,
    )
    problems = list(report["problems"])
    resolved = report["resolved"]
    print("=== 实验配置校验 ===")
    print(f"配置文件  : {args.config}")
    print(
        "实验条件  : "
        f"name={resolved['name']!r} mode={resolved['mode']!r} "
        f"top_k={resolved['top_k']} split_strategy={resolved['split_strategy']!r}"
    )
    print(
        "判据      : "
        f"整体 ≥ 基线 -{resolved['overall_pp']}pp；单题型退化 ≤ {resolved['per_type_pp']}pp"
    )

    # 基线是可选项，但配了就必须能读到——「配了却没生效」会让闸门悄悄退化成「无基线」。
    reference = args.baseline or resolved["baseline"]
    if reference:
        try:
            baseline, source = resolve_baseline(reference=reference)
        except SystemExit as exc:
            problems.append(f"基线读不到：{exc}")
        else:
            if baseline is None:
                problems.append(f"基线 {reference!r} 解析为空")
            else:
                print(
                    f"基线      : {baseline.get('run_id')}（{source}，"
                    f"语料 {baseline.get('corpus', {}).get('version')}，"
                    f"{len((baseline.get('scope') or {}).get('question_ids') or [])} 道题）"
                )
    else:
        print("基线      : 自动（先 eval/baseline/，再退到最近一次全量归档）")

    for problem in problems:
        print(f"PROBLEM   : {problem}")
    if problems:
        print(f"\n结论：不通过（{len(problems)} 处问题）", file=sys.stderr)
        return EXIT_RED
    print("\n结论：通过")
    return EXIT_OK


# ─────────────────────────────────────────────────────────── 子命令：出报告


def _baseline_note(baseline_dir: Path) -> str:
    return "（随仓库走的那份）" if baseline_dir == BASELINE_DIR else "（另存的一份）"


def cmd_report(args: argparse.Namespace) -> int:
    run_dir = resolve_run_dir(args.run)
    payload = read_run(run_dir)
    payload.setdefault("run_dir", _display_path(run_dir))
    # 读与写都走 --baseline-dir 这一个参数：比的是哪个目录，写回去的就是哪个目录
    baseline_dir = resolve_baseline_dir(getattr(args, "baseline_dir", None))

    if args.select:
        try:
            value = select_value(payload, args.select)
        except KeyError as exc:
            print(f"取不到 {args.select}：{exc}", file=sys.stderr)
            return EXIT_FAILED
        if isinstance(value, (dict, list)):
            print(json.dumps(json_safe(value), ensure_ascii=False, indent=2))
        else:
            print(value)
        return EXIT_OK

    if args.baseline or getattr(args, "baseline_dir", None):
        baseline, source = resolve_baseline(reference=args.baseline, baseline_dir=baseline_dir)
        payload["gate"] = compare_to_baseline(
            payload,
            baseline,
            thresholds=_thresholds_from_payload(payload),
            baseline_source=source,
        )

    if args.update_baseline:
        if not (payload.get("scope") or {}).get("full"):
            print("--update-baseline 只接受全量运行", file=sys.stderr)
            return EXIT_FAILED
        # 护栏先于任何一次写盘：被拒的时候，磁盘上应该什么都没变
        problem = baseline_overwrite_problem(
            baseline_dir,
            rerank_enabled=_archived_rerank_enabled(payload),
            force=bool(getattr(args, "force", False)),
        )
        if problem is not None:
            print(problem, file=sys.stderr)
            return EXIT_FAILED

    write_run_dir(run_dir, payload)
    if args.update_baseline:
        write_run_dir(baseline_dir, _promote_baseline(payload, baseline_dir=baseline_dir))
        print(f"基线      : 已写入 {_display_path(baseline_dir)}{_baseline_note(baseline_dir)}")

    verdict = (payload.get("gate") or {}).get("verdict")
    print(f"报告      : {_display_path(run_dir / 'report.md')}")
    print(f"机读结果  : {_display_path(run_dir / 'results.json')}")
    print(f"环境快照  : {_display_path(run_dir / 'environment.json')}")
    print(f"门禁判定  : {verdict}")
    summary = (payload.get("gate") or {}).get("summary")
    if summary:
        print(f"说明      : {summary}")
    return EXIT_OK if verdict in {"pass", "no_baseline", "incomparable"} else EXIT_RED


def _thresholds_from_payload(payload: Mapping[str, Any]) -> GateThresholds:
    thresholds = (payload.get("gate") or {}).get("thresholds") or {}
    return GateThresholds(
        float(thresholds.get("overall_pp", DEFAULT_OVERALL_PP)),
        float(thresholds.get("per_type_pp", DEFAULT_PER_TYPE_PP)),
    )


# ─────────────────────────────────────────────────────────────── 子命令：跑


def _apply_eval_switches(args: argparse.Namespace, *, top_k: int) -> bool:
    """把本轮旋钮钉死（**先于**环境快照），返回**实际生效**的重排开关。

    ``--rerank`` 缺省关：两次冻结基线都是关着重排跑的，历史口径必须逐字节不变。
    返回值不是 ``args.rerank``，而是钉完之后 ``settings`` 上的真值——环境快照要说的是
    「这次到底开没开」，不是「命令行上写没写这个参数」。
    """

    from app.core.config import settings
    from scripts import eval_retrieval as ev

    rerank = bool(getattr(args, "rerank", False))
    ev._isolate_settings(top_k=top_k, rerank=rerank)
    return bool(settings.RERANK_ENABLED)


async def _score_questions(
    db: Any,
    *,
    questions: Sequence[Any],
    labels: Mapping[str, list[str]],
    mode: str,
    top_k: int,
    kb_ids: Sequence[str],
) -> tuple[list[QuestionScore], list[dict[str, Any]], list[str]]:
    """逐题真实检索 + 打分。返回（门禁用的分数、逐题明细、检索失败的题号）。

    只对**进主平均**的题跑：``absent`` / ``false_premise`` 不进闸门，跑它们只是白烧
    嵌入与召回。它们在报告里以「排除」出现，不被悄悄丢掉。
    """

    from scripts import eval_retrieval as ev

    scores: list[QuestionScore] = []
    detail: list[dict[str, Any]] = []
    failures: list[str] = []
    for index, question in enumerate(questions, start=1):
        outcome = await ev._run_question(db, question, mode=mode, top_k=top_k, kb_ids=list(kb_ids))
        reference = [str(chunk_id) for chunk_id in labels.get(question.id, [])]
        reason: str | None = None
        precision: float | None = None
        recall: float | None = None
        if outcome.error is not None:
            reason = f"retrieval_failed: {outcome.error}"
            failures.append(question.id)
        elif not reference:
            # 进主平均的题不该没有标注（validate-dataset 已经拦过一次）；到这里就是语料/标注
            # 在运行期间漂了，如实记「未评分」，绝不按 0 分算。
            reason = "no_reference_ids"
        else:
            precision, recall = await context_scores(outcome.returned_ids, reference)
            if precision is None:
                reason = "no_retrieved_context"
        scores.append(
            QuestionScore(
                id=question.id,
                type=question.type,
                context_precision=precision,
                context_recall=recall,
                unscored_reason=reason,
            )
        )
        detail.append(
            {
                "id": question.id,
                "type": question.type,
                "query": question.query,
                "returned_count": len(outcome.returned_ids),
                "reference_count": len(reference),
                "context_precision": precision,
                "context_recall": recall,
                "unscored_reason": reason,
                "empty_reason": outcome.empty_reason,
                "degraded": outcome.degraded,
                "degraded_reason": outcome.degraded_reason,
                "duration_ms": outcome.duration_ms,
                "returned_ids": outcome.returned_ids,
                "reference_ids": reference,
            }
        )
        print(
            f"[{index}/{len(questions)}] {question.id:<5} {question.type:<14} "
            f"cp={_fmt(precision)} cr={_fmt(recall)} 返回 {len(outcome.returned_ids)} 条"
            + (f"  ({outcome.error})" if outcome.error else "")
        )
    return scores, detail, failures


def _fmt(value: float | None) -> str:
    """报告里的分数渲染：``None`` 印成 ``n/a``，不是 0。

    局部 import：本模块的 ``settings`` 必须晚于 ``--env-file`` 载入（见文件顶部那段），
    而这条渲染函数与配置无关，放在函数里就不必跟着那套时序走。
    """

    from app.utils.report_format import as_metric

    return as_metric(value)


async def _http_smoke(
    *,
    base_url: str,
    endpoint: str,
    api_key: str,
    questions: Sequence[Any],
    kb_ids: Sequence[str],
) -> dict[str, Any]:
    """HTTP 冒烟：只断言字段齐全，不算分、不进闸门（票面第 6 条）。"""

    import httpx

    url = base_url.rstrip("/") + "/" + endpoint.lstrip("/")
    samples: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=60.0) as client:
        for question in questions:
            entry: dict[str, Any] = {"id": question.id, "query": question.query}
            try:
                response = await client.post(
                    url,
                    json={"kb_ids": list(kb_ids), "query": question.query},
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            except Exception as exc:  # noqa: BLE001 - 冒烟失败也是一种观察结果
                entry.update(
                    status_code=None,
                    code=None,
                    returned=0,
                    missing=[f"请求失败：{type(exc).__name__}: {exc}"],
                )
                samples.append(entry)
                continue
            try:
                body = response.json()
            except ValueError:
                body = None
            data = body.get("data") if isinstance(body, dict) else None
            chunks = (data or {}).get("retrieved_chunks") if isinstance(data, dict) else None
            entry.update(
                status_code=response.status_code,
                code=(body or {}).get("code") if isinstance(body, dict) else None,
                returned=len(chunks) if isinstance(chunks, list) else 0,
                missing=missing_response_fields(body),
            )
            samples.append(entry)
    passed = all(
        sample.get("status_code") == 200 and not sample.get("missing") for sample in samples
    )
    return {
        "base_url": base_url,
        "endpoint": endpoint,
        "passed": passed,
        "samples": samples,
        "note": "HTTP 冒烟只验证字段齐全；它不参与门禁，分数以进程内检索那一份为准。",
    }


async def _run(args: argparse.Namespace) -> int:
    config = load_experiment(Path(args.config))
    from app.core.config import SUPPORTED_RETRIEVAL_MODES
    from app.models.knowledge_base import SPLIT_STRATEGIES
    from app.services.retrieval_options import MAX_TOP_K

    config_report = validate_experiment(
        config,
        modes=SUPPORTED_RETRIEVAL_MODES,
        strategies=SPLIT_STRATEGIES,
        top_k_max=MAX_TOP_K,
    )
    if config_report["problems"]:
        raise SystemExit("实验配置不合法：\n  - " + "\n  - ".join(config_report["problems"]))

    resolved = config_report["resolved"]
    mode = str(resolved["mode"])
    top_k = int(args.top_k or resolved["top_k"])
    thresholds = GateThresholds(
        float(resolved["overall_pp"] or DEFAULT_OVERALL_PP),
        float(resolved["per_type_pp"] or DEFAULT_PER_TYPE_PP),
    )

    started = time.perf_counter()
    run_id = args.run_id or utc_stamp()
    run_dir = RUNS_DIR / run_id
    generated_at = datetime.now(UTC).isoformat(timespec="seconds")
    env_file = getattr(args, "env_file", None)
    baseline_dir = resolve_baseline_dir(getattr(args, "baseline_dir", None))

    experiment_block = {
        "name": resolved["name"],
        "mode": mode,
        "top_k": top_k,
        "split_strategy": resolved["split_strategy"],
        "limit": args.limit,
        "gate": thresholds.as_dict(),
    }

    # ── 1. 离线校验题库（不连库）：标注对不上就当场停，别让「检索不行」背锅 ──
    questions_payload = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    snapshot = corpus_snapshot(**_chunking_settings())
    dataset_report = validate_dataset(
        questions_payload, locators=snapshot["locators"], top_k_max=MAX_TOP_K
    )
    if dataset_report["problems"]:
        raise SystemExit(
            "题库校验不通过（先跑 validate-dataset 看明细）：\n  - "
            + "\n  - ".join(dataset_report["problems"][:10])
        )

    dataset_version = corpus_version(
        {**snapshot["files"], "questions.json": QUESTIONS_PATH.read_text(encoding="utf-8")}
    )
    corpus_block = {
        "version": dataset_version,
        "documents": snapshot["documents"],
        "chunks": snapshot["chunks"],
        "kb_ids": snapshot["kb_ids"],
        "questions": dataset_report["stats"]["questions"],
        "answerable": dataset_report["stats"]["answerable"],
        "gate_questions": dataset_report["stats"]["gate"],
        "labels": dataset_report["stats"]["labels"],
    }

    from scripts import eval_retrieval as ev

    # 先把本轮旋钮钉死**再**拍环境快照：顺序反了，快照记的就是 .env 里的偶然值
    # （比如 RERANK_ENABLED=true），而实际跑的是一次重排关闭的检索——环境快照说假话，
    # 比没有环境快照更糟。
    _apply_eval_switches(args, top_k=top_k)
    environment = environment_snapshot(
        run_id=run_id,
        generated_at=generated_at,
        experiment={**experiment_block},
        corpus=corpus_block,
        dataset_version=dataset_version,
        env_file=env_file,
    )

    # ── 2. 真实链路：进程内检索（PG + ES + 嵌入全部是真的） ──
    live = ev._preflight()
    environment["embedding"] = {
        "provider": live.embedding_provider,
        "model": live.embedding_model,
        "dimensions": live.embedding_dimensions,
    }
    environment["keyword"] = {
        "provider": live.keyword_provider,
        "url": live.elasticsearch_url,
        "index": live.elasticsearch_index,
    }

    questions, _question_k = ev._load_questions()
    layouts, _empty_kb_id = ev._load_manifest()
    docs = ev._load_corpus(layouts)
    kb_ids = [layout.kb_id for layout in layouts]

    db = ev.SessionLocal()
    try:
        if not args.reuse:
            es_state = ev._reset_elasticsearch(live.elasticsearch_index)
            pg_counts = ev._reset_postgres(db)
            print(f"reset     : es {es_state}; pg {pg_counts}")
            for layout in layouts:
                ev._ensure_knowledge_base(
                    db,
                    kb_id=layout.kb_id,
                    name=layout.name,
                    description=layout.description,
                    split_strategy=resolved["split_strategy"],
                )
            for doc in docs:
                ev._ensure_document(db, doc)
            indexed = await ev._index_documents(db, docs)
            print(
                f"indexed   : {sum(indexed.values())} chunks in {time.perf_counter() - started:.1f}s"
            )

        chunks = ev._load_indexed_chunks(db)
        labels, _traps = ev._resolve_labels(questions, chunks)
        corpus_block["chunks"] = len(chunks)
        corpus_block["documents"] = len({chunk.document_id for chunk in chunks})

        included = [q for q in questions if q.type not in EXCLUDED_QUESTION_TYPES]
        selected = included[: args.limit] if args.limit else included
        scope_full = args.limit is None and len(selected) == len(included)
        if args.update_baseline and not scope_full:
            raise SystemExit(
                "--update-baseline 只接受全量运行：裁剪运行的题集不能当基线（它会让下一次全量跑不可比）"
            )
        print(
            f"questions : 跑 {len(selected)}/{len(included)} 道进平均的题"
            f"（题库 {len(questions)} 道；absent / false_premise 不进闸门）"
        )

        scores, detail, failures = await _score_questions(
            db,
            questions=selected,
            labels=labels,
            mode=mode,
            top_k=top_k,
            kb_ids=kb_ids,
        )
    finally:
        db.close()

    http_smoke = None
    if args.via_http:
        if not args.api_key:
            raise SystemExit("--via-http 需要 --api-key（或用环境变量 EVAL_API_KEY 提供）")
        smoke_questions = select_smoke_questions(selected, int(resolved["http_samples"] or 3))
        smoke_kb_ids = args.via_http_kb or kb_ids
        print(f"\nhttp smoke: {args.via_http}（{len(smoke_questions)} 道，不进闸门）")
        http_smoke = await _http_smoke(
            base_url=args.via_http,
            endpoint=args.http_endpoint,
            api_key=args.api_key,
            questions=smoke_questions,
            kb_ids=smoke_kb_ids,
        )

    payload = build_results_payload(
        run_id=run_id,
        run_dir=_display_path(run_dir),
        generated_at=generated_at,
        status="ok" if scope_full else "smoke",
        experiment=experiment_block,
        corpus=corpus_block,
        environment=environment,
        scores=scores,
        gate={
            "verdict": "no_baseline",
            "passed": None,
            "reason": None,
            "overall": [],
            "by_type": [],
            "regressions": [],
            "summary": "",
        },
        per_question=detail,
        scope_full=scope_full,
        excluded_types={
            **dataset_report["stats"]["excluded"],
        },
        http_smoke=http_smoke,
        duration_seconds=round(time.perf_counter() - started, 2),
    )
    payload["retrieval_failures"] = failures

    baseline, source = resolve_baseline(
        reference=args.baseline or resolved["baseline"] or None,
        current_run_id=run_id,
        baseline_dir=baseline_dir,
    )
    gate = compare_to_baseline(payload, baseline, thresholds=thresholds, baseline_source=source)
    if not scope_full:
        # 裁剪过的运行：数字照留（它们是真的），但闸门不可用。理由写进 gate，
        # 让报告与机读结果对「这次能不能当闸门」只有一个答案。
        gate["verdict"] = "incomparable"
        gate["passed"] = None
        gate["reason"] = "scope_partial"
        gate["summary"] = (
            f"本次是 --limit {args.limit} 裁剪过的运行：CP/CR 是真的，但不参与门禁"
            "（题集与基线不同，任何差值都不成立）。"
            + (
                f"（与基线逐项对比只作参考：{gate['summary']}）"
                if baseline is not None and gate.get("summary")
                else ""
            )
        )
    payload["gate"] = gate

    write_run_dir(run_dir, payload)
    if args.update_baseline:
        write_run_dir(baseline_dir, _promote_baseline(payload, baseline_dir=baseline_dir))
        print(f"baseline  : 已写入 {_display_path(baseline_dir)}{_baseline_note(baseline_dir)}")

    print("\n=== gate ===")
    overall = payload["metrics"]["overall"]
    print(
        f"context_precision = {_fmt(overall['context_precision'])} "
        f"（已评分 {overall['scored']['context_precision']}/{overall['total']}）"
    )
    print(
        f"context_recall    = {_fmt(overall['context_recall'])} "
        f"（已评分 {overall['scored']['context_recall']}/{overall['total']}）"
    )
    print(f"verdict           = {payload['gate']['verdict']}")
    print(f"summary           = {payload['gate']['summary']}")
    print(f"\nrun dir           : {_display_path(run_dir)}")

    verdict = payload["gate"]["verdict"]
    return EXIT_OK if verdict in {"pass", "no_baseline", "incomparable"} else EXIT_RED


def cmd_run(args: argparse.Namespace) -> int:
    """``run`` 的失败也要留档：一次跑不出数字，同样是「这个日期这个条件下发生了什么」。"""

    if args.limit is not None and args.limit < 1:
        print("--limit 必须是 >=1 的整数", file=sys.stderr)
        return EXIT_FAILED
    if args.update_baseline:
        # 护栏放在跑之前：拒绝覆盖时一行都不该跑，也不该花掉整轮的嵌入与重排钱
        problem = baseline_overwrite_problem(
            resolve_baseline_dir(getattr(args, "baseline_dir", None)),
            rerank_enabled=bool(args.rerank),
            force=bool(getattr(args, "force", False)),
        )
        if problem is not None:
            print(problem, file=sys.stderr)
            return EXIT_FAILED
    # run id 只生成一次：失败档案与正常档案必须落在同一个目录，否则「那一次」会变成两条记录。
    args.run_id = args.run_id or utc_stamp()
    run_dir = RUNS_DIR / args.run_id
    try:
        return asyncio.run(_run(args))
    except SystemExit as exc:
        message = str(exc)
        _archive_failure(run_dir, run_id=args.run_id, stage="preflight", message=message)
        print(f"\n未跑出数字：{message}", file=sys.stderr)
        return EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 - 任何异常都要落成一份可读的失败档案
        message = f"{type(exc).__name__}: {exc}"
        _archive_failure(run_dir, run_id=args.run_id, stage="run", message=message)
        print(f"\n未跑出数字：{message}", file=sys.stderr)
        return EXIT_FAILED


def _archive_failure(run_dir: Path, *, run_id: str, stage: str, message: str) -> None:
    generated_at = datetime.now(UTC).isoformat(timespec="seconds")
    payload = {
        "schema": RUN_SCHEMA,
        "run_id": run_id,
        "run_dir": _display_path(run_dir),
        "generated_at": generated_at,
        "status": "failed",
        "failure": {"stage": stage, "message": message},
        "experiment": {},
        "corpus": {},
        "environment": {
            "run_id": run_id,
            "generated_at": generated_at,
            "runtime": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "git_commit": _git("rev-parse", "--short", "HEAD"),
                "git_dirty": bool(_git("status", "--porcelain")),
            },
        },
        "scope": {"full": False, "question_ids": [], "question_count": 0},
        "metrics": score_run([]),
        "per_question": [],
        "gate": {
            "verdict": "incomparable",
            "passed": None,
            "reason": "run_failed",
            "thresholds": {"overall_pp": DEFAULT_OVERALL_PP, "per_type_pp": DEFAULT_PER_TYPE_PP},
            "baseline": None,
            "overall": [],
            "by_type": [],
            "regressions": [],
            "summary": "运行失败，没有产出任何 CP / CR。",
        },
        "http_smoke": None,
    }
    write_run_dir(run_dir, payload)


# ─────────────────────────────────────────────────────────────── 命令行


def _add_env_file(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--env-file",
        default=None,
        help="dotenv 文件（在 import app.core.config 之前加载；显式环境变量优先）",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval_runner",
        description=("统一评测运行器：校验题库与实验配置、进程内真实检索、确定性门禁、归档报告。"),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    dataset = sub.add_parser("validate-dataset", help="离线校验题库与标注（不连库、不发网络）")
    dataset.add_argument("--questions", default=str(QUESTIONS_PATH))
    dataset.add_argument("--corpus-dir", default=str(CORPUS_DIR))
    dataset.add_argument("--manifest", default=str(MANIFEST_PATH))
    _add_env_file(dataset)
    dataset.set_defaults(handler=cmd_validate_dataset)

    config = sub.add_parser("validate-config", help="校验实验配置与基线可读性")
    config.add_argument("--config", default=str(EXPERIMENT_PATH))
    config.add_argument("--baseline", default=None, help="指定基线（留空 = 用配置里的）")
    _add_env_file(config)
    config.set_defaults(handler=cmd_validate_config)

    run = sub.add_parser("run", help="真实跑一次：校验 → 检索 → 打分 → 比基线 → 归档")
    run.add_argument("--config", default=str(EXPERIMENT_PATH))
    run.add_argument(
        "--limit",
        type=int,
        default=None,
        help="只跑前 N 道进平均的题（验证通路用；这样跑出来的不参与门禁）",
    )
    run.add_argument("--top-k", type=int, default=None, help="覆盖配置里的 top_k")
    run.add_argument(
        "--rerank",
        action="store_true",
        help=(
            "这一轮**开着**重排跑（缺省关：历史口径逐字节不变）。"
            "开/关是门禁尺子上的一根刻度，两种口径不能互相扣分"
        ),
    )
    run.add_argument(
        "--baseline-dir",
        default=None,
        help="基线目录（读与写都走它；缺省 eval/baseline/，另一份可另存如 eval/baseline-rerank）",
    )
    run.add_argument(
        "--baseline",
        default=None,
        help="基线：归档目录 / results.json / run id；'none' 表示本次不比",
    )
    run.add_argument("--reuse", action="store_true", help="复用已索引的语料，不重建")
    run.add_argument("--via-http", default=None, metavar="BASE_URL", help="另跑一条 HTTP 冒烟")
    run.add_argument(
        "--api-key", default=None, help="HTTP 冒烟的 Bearer token（或用 EVAL_API_KEY）"
    )
    run.add_argument(
        "--via-http-kb",
        action="append",
        default=None,
        help="冒烟用的 kb_id（可重复；缺省用全部评测库）",
    )
    run.add_argument("--http-endpoint", default=DEFAULT_HTTP_ENDPOINT)
    run.add_argument(
        "--update-baseline",
        action="store_true",
        help="把这次结果写成基线目录里的那份（缺省 eval/baseline/）",
    )
    run.add_argument(
        "--force",
        action="store_true",
        help="配合 --update-baseline：允许覆盖「重排开关不同」的既有基线（会换掉冻结读数）",
    )
    run.add_argument("--run-id", default=None, help="覆盖 run id（缺省 UTC 时间戳）")
    _add_env_file(run)
    run.set_defaults(handler=cmd_run)

    report = sub.add_parser("report", help="从归档重出报告（--select 单独打印一个数字）")
    report.add_argument("--run", required=True, help="归档目录 / results.json / run id")
    report.add_argument("--select", default=None, help="按 a.b.0.c 取值并打印")
    report.add_argument("--baseline", default=None, help="按指定基线重算门禁")
    report.add_argument(
        "--baseline-dir",
        default=None,
        help="基线目录（读与写都走它；缺省 eval/baseline/）",
    )
    report.add_argument(
        "--update-baseline",
        action="store_true",
        help="把这次归档写成基线目录里的那份（重出报告的同时重钉基线，不重跑检索）",
    )
    report.add_argument(
        "--force",
        action="store_true",
        help="配合 --update-baseline：允许覆盖「重排开关不同」的既有基线（会换掉冻结读数）",
    )
    _add_env_file(report)
    report.set_defaults(handler=cmd_report)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run" and not args.api_key:
        args.api_key = os.environ.get("EVAL_API_KEY") or None
    try:
        env_file = preload_env_file(getattr(args, "env_file", None))
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILED
    setattr(args, "env_file", env_file)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
