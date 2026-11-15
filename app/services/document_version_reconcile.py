"""文档版本对账的判据（#179）：四条不变量，全是**纯函数**。

它回答的问题是「哪两处本该相等的真值，其实不等」。本步把一份正文放在两个地方（版本表是
真值、文档行是快照，D6）、把稿号放在两个地方（切片的稿号、文档上的指针）、把「当前稿」放
在两个地方（库里的指针、关键词索引里的一个布尔），于是有四处会各自漂移，而**每一处漂移
都不会报错**——检索照常返回，只是返回的是另一稿的东西。所以这四条要能一条命令数出来。

四条（父 spec D14）：

====================  ==================================================
检查 1                每个切片的稿号都能找到对应的版本行
检查 2                每篇文档恰好一个当前稿版本行，且等于文档上的指针
检查 3                文档正文与当前稿正文逐字相同
检查 4                关键词索引里标「当前稿」的切片的稿号等于文档指针
====================  ==================================================

## 为什么判据是纯函数

「哪两条数据本该相等」是**口径**，不是 I/O。把它写在脚本里，评测、控制台、将来的
定时对账各自复制一遍，然后四份口径慢慢分叉——这与本仓库「同一件事只有一个判据」
（``app.services.index_verification`` 之于索引计数）是同一条纪律。脚本只管取数与打印。

## 第四条的三种状态，不是两种

关键词索引在另一套系统里（ES 不在任何数据库事务里），所以它天然可能**答不上来**：

* ``KEYWORD_STATE_CHECKED``：查到了，逐篇比；
* ``KEYWORD_STATE_UNDECIDABLE`` + ``KEYWORD_REASON_ABSENT``：这台机器没有关键词能力
  （``KEYWORD_SEARCH_PROVIDER=none``）。**这不是失败**——没有的东西不会跟库不一致；
* ``KEYWORD_STATE_UNDECIDABLE`` + ``KEYWORD_REASON_FAILED``：能力在，这一次读不到
  （集群挂了、超时）。也不是失败，但原因必须与上一种分开打印：一个要装引擎，一个要排障。

把「答不上来」记成通过是撒谎，记成失败会让没部署 ES 的机器永远红——那两种结局都会让这条
脚本被人加上 `|| true`。

## 什么不是分歧

* **某一稿没有切片**：合法（保留窗口清过、重建中途、空文档）。检查 1 只查反方向。
* **指针那一稿没有切片**：D5 的**在途窗口**（指针已前移、新稿的切片还没建好）。它是
  **信息项**：运维据此区分「重建没跑完」与「真的丢了」，但不判失败——切稿之后总要有一
  小段时间是这样。
* **关键词里一篇都没标「当前稿」**：信息项。老文档缺 ``is_current`` 字段（mapping 是后补
  的）时它就是这个样子，而它会让关键词路静默全空——必须被看见，但不是数据分歧。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

#: 检查 1 的名字：每个切片的稿号都能找到版本行。
CHECK_CHUNK_VERSION_ROWS = "chunk-version-row"
#: 检查 2 的名字：每篇文档恰好一个当前稿，且等于文档指针。
CHECK_CURRENT_DRAFT = "current-draft"
#: 检查 3 的名字：文档正文与当前稿正文逐字相同。
CHECK_CONTENT_SNAPSHOT = "content-snapshot"
#: 检查 4 的名字：关键词索引里「当前稿」的稿号等于文档指针。
CHECK_KEYWORD_CURRENT_DRAFT = "keyword-current-draft"

#: 关键词那一维查过了。
KEYWORD_STATE_CHECKED = "checked"
#: 关键词那一维答不上来（能力不存在 / 这一次读不到）。
KEYWORD_STATE_UNDECIDABLE = "undecidable"

#: 不可判定的原因之一：**能力不存在**（没有部署关键词引擎）。这不是数据问题。
KEYWORD_REASON_ABSENT = "keyword search capability is not available"
#: 不可判定的原因之二：能力在，这一次读不到（集群故障 / 超时）。这是运维问题。
KEYWORD_REASON_FAILED = "keyword search read failed"

#: 版本行的状态取值（与 ``app.models.document_version`` 同一份词表）。
_STATUS_CURRENT = "current"


@dataclass(frozen=True)
class DocumentFacts:
    """一篇文档参与对账的全部读数：**一行文档 + 它的版本行 + 它的切片 + 关键词标记**。

    字段全是「已经取回来的事实」，判据不做任何 I/O，也不去补缺失值：

    ``version_rows``      ``(稿号, 状态, 正文)`` 三元组，一篇的全部版本行
    ``chunk_versions``    ``(稿号, 这一稿有几片)``，按稿号分组后的切片读数
    ``keyword_current_versions``
                          关键词索引里标着「当前稿」的**稿号集合**；``None`` = 这一维
                          答不上来（不可判定），空元组 = 查过、一篇都没标
    """

    document_id: str
    pointer: int
    content: str
    version_rows: tuple[tuple[int, str, str], ...]
    chunk_versions: tuple[tuple[int, int], ...]
    keyword_current_versions: tuple[int, ...] | None

    @property
    def chunks(self) -> int:
        """这篇文档的切片总数（打印「看了多少片」用）。"""

        return sum(count for _version, count in self.chunk_versions)

    @property
    def current_rows(self) -> tuple[tuple[int, str, str], ...]:
        """状态是 ``current`` 的那些版本行。"""

        return tuple(row for row in self.version_rows if row[1] == _STATUS_CURRENT)

    def chunks_of(self, version: int) -> int:
        """第 ``version`` 稿有几片（没读过就是 0）。"""

        return dict(self.chunk_versions).get(version, 0)


@dataclass(frozen=True)
class Discrepancy:
    """一条分歧：哪篇文档、差在哪。打印时一行一条（可以贴进工单）。"""

    document_id: str
    detail: str

    def render(self) -> str:
        return f"{self.document_id}  {self.detail}"


@dataclass(frozen=True)
class ReconcileReport:
    """一次对账的全部读数。``checks`` 的顺序就是打印顺序（运维按它读）。"""

    documents: int
    chunks: int
    chunk_version_rows: tuple[Discrepancy, ...]
    current_drafts: tuple[Discrepancy, ...]
    content_snapshots: tuple[Discrepancy, ...]
    keyword_current_drafts: tuple[Discrepancy, ...]
    keyword_state: str
    keyword_reason: str | None
    #: 信息项（不判失败）：指针那一稿没有切片（D5 的在途窗口）。
    pointer_drafts_without_chunks: tuple[Discrepancy, ...]
    #: 信息项（不判失败）：关键词索引里这一篇没有任何「当前稿」切片。
    keyword_drafts_without_current: tuple[Discrepancy, ...]

    @property
    def checks(self) -> tuple[tuple[str, tuple[Discrepancy, ...]], ...]:
        """四条检查的名字与分歧，**按打印顺序**。"""

        return (
            (CHECK_CHUNK_VERSION_ROWS, self.chunk_version_rows),
            (CHECK_CURRENT_DRAFT, self.current_drafts),
            (CHECK_CONTENT_SNAPSHOT, self.content_snapshots),
            (CHECK_KEYWORD_CURRENT_DRAFT, self.keyword_current_drafts),
        )

    @property
    def disagreements(self) -> int:
        """四条检查的分歧总数（信息项不算）。"""

        return sum(len(items) for _name, items in self.checks)

    @property
    def has_disagreements(self) -> bool:
        return self.disagreements > 0

    @property
    def keyword_undecidable(self) -> bool:
        return self.keyword_state == KEYWORD_STATE_UNDECIDABLE


# ─────────────────────────────────────────────────────────────── 四条判据


def chunk_version_row_disagreements(facts: DocumentFacts) -> tuple[Discrepancy, ...]:
    """检查 1：切片上的每个稿号，版本表里都要有那一行。

    只看**切片 → 版本行**这个方向：某一稿今天没有切片是合法的（保留窗口清过、重建中途、
    空文档），而切片指向一稿谁也读不到则意味着这一片永远查不回来是哪一稿的。
    """

    known = {version for version, _status, _content in facts.version_rows}
    missing = sorted(version for version, _count in facts.chunk_versions if version not in known)
    if not missing:
        return ()
    return (
        Discrepancy(
            document_id=facts.document_id,
            detail=(
                f"切片指向的稿号没有版本行：{', '.join(str(version) for version in missing)}"
                f"（版本表里只有 {', '.join(str(version) for version in sorted(known)) or '空'}）"
            ),
        ),
    )


def current_draft_disagreements(facts: DocumentFacts) -> tuple[Discrepancy, ...]:
    """检查 2：**恰好一条** ``current`` 版本行，且它的稿号等于文档上的指针。

    两条口径分开看，因为它们是两种不同的坏法：

    * 「单活」破了（0 条或 2 条以上当前稿）——谁是真值本身就不成立；
    * 单活成立、但**指针指的不是它**——检索（按指针过滤）与对账（按状态比较）会对同一
      篇文档给出两个答案，而检索永远不会报错，它只是查另一稿。
    """

    current = sorted(version for version, _status, _content in facts.current_rows)
    if len(current) != 1:
        return (
            Discrepancy(
                document_id=facts.document_id,
                detail=(
                    f"当前稿版本行 = {current or '空'}（期望恰好一条；指针 = {facts.pointer}）"
                ),
            ),
        )
    if current[0] != facts.pointer:
        return (
            Discrepancy(
                document_id=facts.document_id,
                detail=f"当前稿 = {current[0]}，文档指针 = {facts.pointer}（检索按指针，对账按状态）",
            ),
        )
    return ()


def content_snapshot_disagreements(facts: DocumentFacts) -> tuple[Discrepancy, ...]:
    """检查 3：``documents.content``（快照）与当前稿正文（真值）**逐字**相同。

    「当前稿不唯一 / 不存在」的文档在这里**跳过**：那是检查 2 的根因，同一条根因在两个
    计数里各报一次，只会让「有几处分歧」这个数字失去意义。

    逐字比较、不做任何规范化：规范化只在写入时做一次（``app.utils.checksum`` 走同一个
    入口），读出来不一样就是不一样——把差异「抹平」等于把这条判据关掉。
    """

    current = facts.current_rows
    if len(current) != 1:
        return ()
    version, _status, body = current[0]
    if facts.content == body:
        return ()
    offset = _first_difference(facts.content, body)
    return (
        Discrepancy(
            document_id=facts.document_id,
            detail=(
                f"文档正文与第 {version} 稿正文不一致"
                f"（documents.content {len(facts.content)} 字符 / 版本行 {len(body)} 字符，"
                f"第 {offset} 个字符起不同）"
            ),
        ),
    )


def keyword_current_draft_disagreements(facts: DocumentFacts) -> tuple[Discrepancy, ...]:
    """检查 4：关键词索引里 ``is_current=true`` 的切片，稿号等于文档指针（跨系统）。

    ``keyword_current_versions`` 是 ``None``（不可判定）时这里没有判据可用，空手而归——
    **不可判定的状态记在报表上**（``keyword_state``），不是记成「通过」，也不是记成「失败」。
    """

    if facts.keyword_current_versions is None:
        return ()
    wrong = sorted(version for version in facts.keyword_current_versions if version != facts.pointer)
    if not wrong:
        return ()
    return (
        Discrepancy(
            document_id=facts.document_id,
            detail=(
                f"关键词索引里标着「当前稿」的切片属于第 "
                f"{', '.join(str(version) for version in wrong)} 稿，文档指针 = {facts.pointer}"
            ),
        ),
    )


# ────────────────────────────────────────────────────────── 信息项（不判失败）


def pointer_drafts_without_chunks(facts: DocumentFacts) -> tuple[Discrepancy, ...]:
    """信息项：指针指的那一稿**没有切片**（D5 的在途窗口：切稿已提交、重建还没跑完）。

    不判失败：那个空窗是 D5 明确接受的代价。它的用处是让运维一眼分开「重建没跑完」
    （这条有、检查 1～3 全绿）与「真的丢了」。
    """

    if facts.chunks_of(facts.pointer):
        return ()
    return (
        Discrepancy(
            document_id=facts.document_id,
            detail=f"指针那一稿（第 {facts.pointer} 稿）没有切片：重建可能还在跑，或这一篇本来没有内容",
        ),
    )


def keyword_drafts_without_current(facts: DocumentFacts) -> tuple[Discrepancy, ...]:
    """信息项：这个文档在关键词索引里**一篇都没标「当前稿」**。

    老物理索引里的文档没有 ``is_current`` 字段（mapping 是后补的，见
    ``build_mapping_update``），症状永远是「关键词路静默全空」——必须被看见，但它不是
    数据分歧（关键词索引里本来就没有那一篇，谈不上「跟库不一致」）。
    """

    if facts.keyword_current_versions is None:
        return ()
    if facts.keyword_current_versions:
        return ()
    return (
        Discrepancy(
            document_id=facts.document_id,
            detail="关键词索引里没有标成「当前稿」的切片（老文档缺 is_current，或还没写进去）",
        ),
    )


# ────────────────────────────────────────────────────────────── 汇总


def reconcile(
    documents: Iterable[DocumentFacts],
    *,
    keyword_state: str = KEYWORD_STATE_CHECKED,
    keyword_reason: str | None = None,
) -> ReconcileReport:
    """把一批文档的读数判成一张报表（四次遍历，$O(n)$）。

    ``keyword_state`` 缺省是「查过」：直接喂读数的那种调用（用例、将来的控制台）不必
    关心这一维是怎么取来的；脚本在取不到时显式传 ``UNDECIDABLE`` 与原因。
    """

    facts = tuple(documents)
    return ReconcileReport(
        documents=len(facts),
        chunks=sum(item.chunks for item in facts),
        chunk_version_rows=_collect(facts, chunk_version_row_disagreements),
        current_drafts=_collect(facts, current_draft_disagreements),
        content_snapshots=_collect(facts, content_snapshot_disagreements),
        keyword_current_drafts=_collect(facts, keyword_current_draft_disagreements),
        keyword_state=keyword_state,
        keyword_reason=keyword_reason,
        pointer_drafts_without_chunks=_collect(facts, pointer_drafts_without_chunks),
        keyword_drafts_without_current=_collect(facts, keyword_drafts_without_current),
    )


def keyword_state_of(
    *, available: bool, error: str | None = None
) -> tuple[str, str | None]:
    """关键词那一维的判定：能力在不在、这一次读没读到 → ``(状态, 原因)``。

    三种组合只有两种状态（``CHECKED`` / ``UNDECIDABLE``），而**原因**要分开：
    ``available=False`` 是「这台机器没有这项能力」，``error`` 非空是「有，但这次读不到」。
    两者都不可判定，但前者不需要排障，后者需要。
    """

    if not available:
        return KEYWORD_STATE_UNDECIDABLE, KEYWORD_REASON_ABSENT
    if error is not None:
        return KEYWORD_STATE_UNDECIDABLE, KEYWORD_REASON_FAILED
    return KEYWORD_STATE_CHECKED, None


# ────────────────────────────────────────────────────────────── 小工具


def _collect(
    facts: Sequence[DocumentFacts], judge: Callable[[DocumentFacts], tuple[Discrepancy, ...]]
) -> tuple[Discrepancy, ...]:
    """逐篇判、按文档顺序摊平（顺序稳定，打印与用例都按它读）。"""

    found: list[Discrepancy] = []
    for item in facts:
        found.extend(judge(item))
    return tuple(found)


def _first_difference(left: str, right: str) -> int:
    """两个字符串第一个不同的位置（0 基）；一个是另一个的前缀时返回较短者的长度。"""

    for index, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return index
    return min(len(left), len(right))


__all__ = [
    "CHECK_CHUNK_VERSION_ROWS",
    "CHECK_CONTENT_SNAPSHOT",
    "CHECK_CURRENT_DRAFT",
    "CHECK_KEYWORD_CURRENT_DRAFT",
    "KEYWORD_REASON_ABSENT",
    "KEYWORD_REASON_FAILED",
    "KEYWORD_STATE_CHECKED",
    "KEYWORD_STATE_UNDECIDABLE",
    "Discrepancy",
    "DocumentFacts",
    "ReconcileReport",
    "chunk_version_row_disagreements",
    "content_snapshot_disagreements",
    "current_draft_disagreements",
    "keyword_current_draft_disagreements",
    "keyword_drafts_without_current",
    "keyword_state_of",
    "pointer_drafts_without_chunks",
    "reconcile",
]
