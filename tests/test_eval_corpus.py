"""语料 v2 与题目的离线守卫：把「设计约束」变成会红的断言。

为什么这些约束要写进测试，而不是只写在 ACCEPTANCE.md 里：v1 的语料同样「看起来没问题」，
真正让测量失效的是**规模**与**标题里带编号**这两件结构事实——它们不会让任何用例变红，
只会让两种模式同时拿到 1.0000。约束一旦只写在文档里，下一个人改语料时不会知道
自己把测量能力改没了。

这里只走纯函数：真切块器（``MarkdownStructuredSplitter``）+ JSON 解析，不连库、不发网络。
"""

from __future__ import annotations

import difflib
import json
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

import pytest

from app.utils.markdown_splitter import MarkdownStructuredSplitter

EVAL_DIR = Path(__file__).resolve().parents[1] / "eval"
CORPUS_DIR = EVAL_DIR / "corpus"
QUESTIONS_PATH = EVAL_DIR / "questions.json"
MANIFEST_PATH = CORPUS_DIR / "manifest.json"

# 与生产默认值一致；显式写出来，避免本测试跟着 .env 的偶发配置漂移。
CHUNK_SIZE = 512
CHUNK_OVERLAP = 50
TABLE_MAX_ROWS = 10

# 题型清单分两层：`REQUIRED_TYPES` 是「每种至少 3 题」的底线，加进来就等于承诺长期维护；
# `ALLOWED_TYPES` 是白名单，防止有人随手写个新 type 却没有任何断言消费它。
#
# 第 3 步扩充题库（26 → 69）时按 RGB 的子任务框架补了四种：
# - `noise_robust` 同族相似章节的精细区分（RGB 的 Noise Robustness）
# - `counterfactual` 同代多档/新旧规则叠加（RGB 的 Counterfactual Robustness）
# - `table_lookup` 数值与时长的精确检索
# - `negative_scope` 否定式提问，答案藏在排除句里
#
# 语料扩到 15 篇后（69 → 107）又补了 `false_premise`：问题本身的前提是错的
# （例如「标准款怎么配双机热备」，而标准款根本不支持），借的是 CRAG 的题型。
REQUIRED_TYPES = (
    "exact_code",
    "model_number",
    "colloquial",
    "synonym",
    "cross_section",
    "absent",
    "noise_robust",
    "counterfactual",
    "table_lookup",
    "negative_scope",
    "false_premise",
)
ALLOWED_TYPES = frozenset((*REQUIRED_TYPES, "rare_token"))

# 标题里一律不许出现的「判别性编号」：错误码、内部项目代号、型号族。
FORBIDDEN_IN_HEADING = (
    re.compile(r"\b[EW]\d{4}\b"),  # E1024 / W0001
    re.compile(r"\bPRJ-[A-Z0-9]{6}\b"),  # PRJ-XXXXXX 随机串
    re.compile(r"\bXR-\d{4}\b"),  # XR-2000 / XR-2100
)

TWIN_CODES = ("E1024", "E1025")


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _documents() -> list[str]:
    """清单里声明的全部语料文件名（决定「哪些文件算评测语料」）。"""

    layout = _manifest()
    return [name for kb in layout["kbs"] for name in kb["documents"]]


def _pieces() -> list[tuple[str, str, str, dict[str, object]]]:
    """(document stem, heading_path, text, metadata) —— 走真切块器，不自己数标题。"""

    splitter = MarkdownStructuredSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP, table_max_rows=TABLE_MAX_ROWS
    )
    pieces = []
    for filename in _documents():
        path = CORPUS_DIR / filename
        assert path.is_file(), f"清单声明了 {filename}，但文件不存在"
        text = path.read_text(encoding="utf-8")
        for piece in splitter.split(text):
            metadata = dict(piece.metadata)
            pieces.append((path.stem, str(metadata["heading_path"]), piece.text, metadata))
    return pieces


def test_corpus_is_a_synthetic_eval_set() -> None:
    """语料必须是合成的：出题人自己写的，不是从真实工单里搬出来的。"""

    stems = {Path(name).stem for name in _documents()}
    # 票 #64：新增一篇多语言代码语料后是 21 篇。上界随语料扩充调整，
    # 下界不动——少于 6 篇时，主题重叠不足，测不出跨文档混淆。
    assert 6 <= len(stems) <= 21, f"语料应为 6-21 篇，实际 {sorted(stems)}"


def test_code_block_corpus_is_in_the_eval_scope() -> None:
    """三类围栏都要真的落在评测语料里，且文档被 manifest 的某个库收录。

    票 #64 的验收点不是「tests/fixtures 里有一份 markdown」，而是切块评测读到的
    ``eval/corpus/*.md`` 里也有：python / json 走 ``from_language`` 的语法分隔符，
    无标记围栏回退正文分隔符，少任何一类都测不到那条分支。只数围栏还不够，
    还要钉住 manifest——``eval_retrieval.py`` 与 ``eval_chunking.py`` 都只从
    ``kbs[].documents`` 派生语料清单，没登记的文件不会被索引、也不算「评测范围内」。
    """

    fences = _opening_fences(CORPUS_DIR.glob("*.md"))
    for language in ("python", "json", ""):
        label = language or "无语言标记"
        assert fences[language] >= 2, f"{label}代码块不足 2 个：{dict(fences)}"

    assert "dev_code_examples.md" in _documents(), "新代码语料未登记进任何 manifest 知识库"
    assert "dev_code_examples.md" in {
        name
        for kb in _manifest()["kbs"]
        if kb["kb_id"] == "kb-eval-ref"
        for name in kb["documents"]
    }, "dev_code_examples.md 应登记在接口与配置参考库"


def _opening_fences(paths: Iterable[Path]) -> Counter[str]:
    """数每篇 md 的开围栏，按围栏上的第一个词归类；无词归到空串。

    只认行首三反引号，并成对跳过闭围栏：正文里的行内反引号不算代码块，
    一块代码也只计一次。刻意不复用切块器的私有解析函数——这条测试要独立于
    被测代码，才能发现「切块器没认出围栏」这类问题。
    """

    counts: Counter[str] = Counter()
    for path in paths:
        inside = False
        for line in path.read_text(encoding="utf-8").splitlines():
            fence = line.lstrip()
            if not fence.startswith("```"):
                continue
            if inside:
                inside = False
                continue
            inside = True
            marker = fence[len("```") :].strip().split()
            counts[marker[0].lower() if marker else ""] += 1
    return counts


def test_chunk_count_stays_in_the_discriminating_range() -> None:
    """200-800 片：这是 ``Recall@20`` 不再覆盖全库的前提。

    v1 只有 39 片，每路召回深度 20，两路一捞就覆盖大半个库，所以两种模式同时满分。
    这不是「实现已经完美」，而是**测量工具没有量程**。上界同样重要：语料太大时
    嵌入成本与单次评测时长会让人不想重跑，评测也就不会更新。

    上界从 400 放宽到 800：h3 成为切片边界、表格加上行级片之后，同一份语料自然
    切出更多片（456 片）。量程要看的是「top_k 能不能蒙中」，不是绝对片数——
    800 片时 top-5 仍只覆盖 0.6%。
    """

    total = len(_pieces())
    assert 200 <= total <= 800, f"切片数 {total} 不在 200-800 之间"


def test_headings_never_carry_discriminating_identifiers() -> None:
    """编号只许出现在正文。

    检索文本 = 文档名 + 章节路径 + 正文（票 2 的设计），章节路径进向量输入也进 BM25。
    标题里写 ``E1024``，等于把编号直接塞给嵌入模型，BM25 的字面优势被抹平——v1 的
    ``E1024`` 就写在 ``## 错误码 E1024：...`` 里。
    """

    offenders: list[str] = []
    for filename in _documents():
        for line_no, line in enumerate(
            (CORPUS_DIR / filename).read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.lstrip().startswith("#"):
                continue
            for pattern in FORBIDDEN_IN_HEADING:
                match = pattern.search(line)
                if match:
                    offenders.append(f"{filename}:{line_no} {line.strip()!r} -> {match.group(0)}")
    assert not offenders, "标题里出现了判别性编号：\n  - " + "\n  - ".join(offenders)


def test_heading_paths_are_unique_inside_each_document() -> None:
    """同一篇里不能有两个同名标题路径。

    注意查的是**源文档的标题**，不是切片的 ``heading_path``：一节被切成多片
    （表格、代码块各自成片，或超长节二次切）是正常的，那些片的 ``heading_path``
    本来就相同。真正会让标注失效的是「文档里有两个同名标题」——那时
    ``(document, heading_path)`` 指向两个不同位置，标签就变成二选一了。
    """

    duplicated: list[str] = []
    for path in _corpus_paths():
        counter = Counter(_heading_paths(path.read_text(encoding="utf-8")))
        duplicated.extend(
            f"{path.stem} :: {heading_path}（{count} 次）"
            for heading_path, count in counter.items()
            if count > 1
        )
    assert not duplicated, "同一篇里出现同名标题：\n  - " + "\n  - ".join(sorted(duplicated))


def test_twin_pair_is_ninety_percent_identical() -> None:
    """``E1024`` / ``E1025`` 两段必须 90% 相同、只差编号与结论句。

    这是「向量分不开、关键词能分开」的物理前提：文字几乎一样，嵌入几乎一样；
    唯一的判别信号是那串编号。
    """

    pieces = _pieces()
    sections: dict[str, str] = {}
    for _stem, _heading, text, _meta in pieces:
        for code in TWIN_CODES:
            if f"{code} " in text and code not in sections:
                body = "\n".join(
                    line for line in text.splitlines() if not line.lstrip().startswith("#")
                )
                sections[code] = body
    assert set(sections) == set(TWIN_CODES), f"找不到孪生对正文：{sorted(sections)}"

    normalized = [re.sub(r"\bE10(24|25)\b", "CODE", sections[code]) for code in TWIN_CODES]
    ratio = difflib.SequenceMatcher(None, *normalized).ratio()
    assert ratio >= 0.9, f"孪生对正文相似度只有 {ratio:.3f}，不足以让向量分不开"


def test_synonym_variants_appear_across_documents() -> None:
    """同一件事要换着说法写在不同篇里，给向量制造真实竞争者（退款 / 退还 / 退钱 / 退费）。"""

    documents = [path.read_text(encoding="utf-8") for path in _corpus_paths()]
    variants = ("退款", "退还", "退钱", "退费")
    seen = {variant: sum(1 for text in documents if variant in text) for variant in variants}
    missing = [variant for variant, count in seen.items() if count == 0]
    assert not missing, f"同义表述缺失：{missing}"
    spread = [variant for variant, count in seen.items() if count >= 2]
    assert len(spread) >= 2, f"同义表述不够分散（至少两种跨 2 篇以上）：{seen}"


def test_model_family_is_present_with_suffix_sensitive_answers() -> None:
    """型号族 ``XR-2000`` / ``XR-2000 Pro`` / ``XR-2100`` 必须都在正文里，
    且后缀差异要真的改变答案（否则型号题没有区分力）。"""

    text = "\n".join(path.read_text(encoding="utf-8") for path in _corpus_paths())
    for model in ("XR-2000", "XR-2000 Pro", "XR-2100"):
        assert model in text, f"语料里缺型号 {model}"
    # 三款的千兆网口数刻意不同：问「XR-2000 Pro 有几个千兆网口」时，
    # 召回 XR-2000 或 XR-2100 的规格段就是**答错**，不是「差一点」。
    expected = {
        "XR-2000 出厂配置 4 个千兆网口": False,
        "XR-2000 Pro 出厂配置 6 个千兆网口": False,
        "XR-2100 出厂配置 8 个千兆网口": False,
    }
    for sentence in expected:
        expected[sentence] = sentence in text
    missing = [sentence for sentence, found in expected.items() if not found]
    assert not missing, f"型号规格句缺失：{missing}"


def test_random_project_codes_exist_at_least_twice() -> None:
    """至少两个 ``PRJ-XXXXXX``：嵌入模型没见过的随机串，是「罕见词盲区」的干净测试。"""

    text = "\n".join(path.read_text(encoding="utf-8") for path in _corpus_paths())
    codes = set(re.findall(r"\bPRJ-[A-Z0-9]{6}\b", text))
    assert len(codes) >= 2, f"随机项目代号不足两个：{sorted(codes)}"


# ───────────────────────────────────────── 不规整标识符（v2 补充，票 8 复核）

# 三类「不规整」标识符：纯哈希、大小写混排、中文与字母数字混排（带连字符）。
# 它们补的是**同一种失败方式的不同侧脸**：向量分不开的到底是什么。
HASH_PATTERN = re.compile(r"\bsha256:([0-9a-f]{16,})\b")
CJK_CODE_PATTERN = re.compile(r"隐患-\d{4}-\d{4}-[A-Z]\b")


def test_irregular_identifiers_cover_hash_case_and_cjk_mixed_numbering() -> None:
    """三类不规整标识符都要在语料里真实出现过。

    为什么要单独补这三类：v2 的编号题都长一个样（``E1024`` 这种「字母 + 四位数字」），
    而真实工单里的标识符长得乱得多——纯哈希串、大小写在不同文档里不一致的代号、
    中文与字母数字混排还带连字符的编号。只测一种形状，等于只验证了最友好的那种。
    """

    text = "\n".join(path.read_text(encoding="utf-8") for path in _corpus_paths())

    hashes = set(HASH_PATTERN.findall(text))
    assert len(hashes) >= 2, f"纯哈希串不足两个：{sorted(hashes)}"
    assert any(len(digest) >= 32 for digest in hashes), "哈希串太短，不像真实摘要"

    assert CJK_CODE_PATTERN.search(text), "缺「中文 + 字母数字 + 连字符」混排的编号"


def test_one_identifier_is_spelled_with_different_case_across_documents() -> None:
    """同一个串在两篇文档里大小写不一致——这是真实工单里最常见的不规整。

    它同时钉住一件事：IK 会把字母串统一成小写（实测 ``PRJ-7F3A91`` 与 ``prj-7f3a91``
    产出同一个 token），所以「大小写不一致」不会让关键词一路失明；真正吃苦头的是
    只按字符串算相似度、又没被显式规范化的那类处理。
    """

    spellings: dict[str, dict[str, set[str]]] = {}
    for path in _corpus_paths():
        for token in re.findall(r"\b[A-Za-z]{2,4}-[0-9A-Za-z]{5,8}\b", path.read_text("utf-8")):
            by_case = spellings.setdefault(token.lower(), {})
            by_case.setdefault(token, set()).add(path.stem)

    inconsistent = {key: cases for key, cases in spellings.items() if len(cases) >= 2}
    assert inconsistent, f"没有跨文档大小写不一致的标识符：{sorted(spellings)}"

    # 至少有一组既换了大小写、又落在不同文档里（同一篇里换来换去不算「跨文档」）。
    cross_document = {
        key: cases
        for key, cases in inconsistent.items()
        if len({doc for docs in cases.values() for doc in docs}) >= 2
    }
    assert cross_document, f"大小写不一致只发生在单篇文档内：{inconsistent}"


def test_hash_twin_pair_differs_only_by_the_digest() -> None:
    """哈希孪生对：两片正文 90% 相同，唯一的判别信号是那串哈希。

    与 ``E1024`` / ``E1025`` 那对是同一个机制，只是换成嵌入模型完全没见过的串：
    如果两片正文几乎一样而向量仍然分得开，那说明它靠的不是「读懂内容」；
    反过来，如果它分不开，就证明「向量分不开的是内容近邻，不是标识符本身」。
    """

    sections: dict[str, str] = {}
    for _stem, _heading, text, _meta in _pieces():
        for digest in HASH_PATTERN.findall(text):
            if digest not in sections:
                body = "\n".join(
                    line for line in text.splitlines() if not line.lstrip().startswith("#")
                )
                sections[digest] = body
    assert len(sections) >= 2, f"哈希孪生对不足两片：{sorted(sections)}"

    digests = sorted(sections)[:2]
    normalized = [re.sub(r"sha256:[0-9a-f]{16,}", "HASH", sections[d]) for d in digests]
    ratio = difflib.SequenceMatcher(None, *normalized).ratio()
    assert ratio >= 0.9, f"哈希孪生对正文相似度只有 {ratio:.3f}，不足以让向量分不开"


# ───────────────────────────────────────────────────────────────── 题目


def _questions() -> dict:
    return json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))


def _corpus_paths() -> list[Path]:
    return [CORPUS_DIR / name for name in _documents()]


def _heading_paths(text: str) -> list[str]:
    """从源文档里抽出每一行标题的完整路径（h1 > h2 > h3 > h4）。

    刻意不复用切块器的实现：这条测试要独立于被测代码，才能发现「切块器把
    路径拼错了」这类问题。切块器只按 h1/h2 分节，这里不关心分节，只关心
    文档里到底有没有两个同名标题。
    """

    stack: list[tuple[int, str]] = []
    paths: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        match = re.match(r"^(#{1,6})\s+(\S.*)$", line)
        if not match:
            continue
        level, title = len(match.group(1)), match.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        paths.append(" > ".join(name for _, name in stack))
    return paths


def test_question_count_and_type_coverage() -> None:
    payload = _questions()
    questions = payload["questions"]
    # 上界随题库扩充调整：第 3 步做切块自评时从 26 道扩到 69 道（按 RGB 子任务补四种题型），
    # 语料扩到 15 篇后又补到 107 道（新增 false_premise 与更多跨文档多跳）。
    # 下界不动——题目太少时测量没有量程。
    assert 20 <= len(questions) <= 140, f"题目数 {len(questions)} 不在 20-140 之间"

    types = Counter(question["type"] for question in questions)
    assert set(types) <= ALLOWED_TYPES, f"出现未声明的题型：{sorted(set(types) - ALLOWED_TYPES)}"
    for required in REQUIRED_TYPES:
        assert types[required] >= 3, f"题型 {required} 只有 {types[required]} 题，至少要 3 题"


def test_absent_questions_have_no_labels_and_are_excluded_from_the_main_average() -> None:
    for question in _questions()["questions"]:
        if question["type"] == "absent":
            assert question["relevant"] == [], f"{question['id']}: absent 题不该有标注"


def test_exact_code_questions_include_the_trap_pair_both_ways() -> None:
    """问 ``E1025`` 时要能测「会不会召回 ``E1024``」，反之亦然。"""

    by_trap: dict[str, str] = {}
    for question in _questions()["questions"]:
        if question["type"] != "exact_code":
            continue
        for trap in question.get("trap", []):
            by_trap.setdefault(trap["heading_path"], question["id"])
    assert len(by_trap) >= 2, f"陷阱对没有双向覆盖：{by_trap}"


def test_every_label_resolves_to_at_least_one_chunk_offline() -> None:
    """题目标注在**离线**就能对上是本测试存在的意义：先于任何一次真实评测失败。

    金标写的是「答案在这一节」，所以一个金标可能命中多片：一节超过 ``chunk_size``
    被二次切成相邻两片、或一节里既有整表片又有一堆行级片，这些片都算相关。
    判定标准因此是「至少 1 片，且不超过 12 片」——上界取 12 是因为
    ``table_max_rows=10`` 下一个表格组最多展开成 1 整表片 + 10 行级片；
    超过它就说明金标写得过粗，指向了整篇文档而不是某一节。

    trap 的上界与金标相同：陷阱节如果含表格，同样会展开成「整表片 + 行级片」。
    陷阱的作用是「这一节看着像、但答不是」，它是不是单点与该性质无关。
    """

    by_locator: Counter[tuple[str, str]] = Counter()
    for stem, heading_path, _text, _meta in _pieces():
        by_locator[(stem, heading_path)] += 1

    problems: list[str] = []
    for question in _questions()["questions"]:
        for label in question["relevant"]:
            key = (label["document"], label["heading_path"])
            count = by_locator[key]
            if count < 1 or count > 12:
                problems.append(f"{question['id']}: {key} 匹配到 {count} 片（应为 1-12）")
        for label in question.get("trap", []):
            key = (label["document"], label["heading_path"])
            count = by_locator[key]
            if count < 1 or count > 12:
                problems.append(f"{question['id']}: trap {key} 匹配到 {count} 片（应为 1-12）")
        for label in question.get("trap", []):
            trap_key = (label["document"], label["heading_path"])
            for gold in question["relevant"]:
                gold_key = (gold["document"], gold["heading_path"])
                assert trap_key != gold_key, f"{question['id']}: 陷阱与金标是同一节"
    assert not problems, "标注与切片对不上：\n  - " + "\n  - ".join(problems)


@pytest.mark.parametrize("model", ["XR-2000", "XR-2000 Pro", "XR-2100"])
def test_model_number_questions_stay_distinguishable(model: str) -> None:
    """同一个问题问三个型号，规格句必须各自独立存在；否则型号题只是在考「同义词」。"""

    text = "\n".join(path.read_text(encoding="utf-8") for path in _corpus_paths())
    assert f"{model} 出厂配置" in text, f"语料里缺 {model} 的整机配置句"
