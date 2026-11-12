"""文档业务：异步上传登记 + 准入判定 + 状态机。

上传只做登记：校验知识库 → **判定格式准入** → 建 ``Document(status=PROCESSING)``
→ 文件模式落盘 / JSON 模式直接落正文 → 登记「提交后投递索引任务」 → 立即返回。
**请求线程不解析、不向量化**，进程内的耗时与文档大小无关；进度由
``GET /documents?kb_id=`` 轮询，后端的解析 / 切块 / 向量化与失败留痕、脱敏文案
统一由 T4 的 Worker 任务负责。提交由**请求边界**做（路由声明 ``Depends(get_transaction)``），
这里只登记「提交之后投递」——本模块不提交，也不允许别的服务在这里顺手提交。

准入判定只有一处实现（:func:`admit_upload`），JSON 传文本与 multipart 传文件两个
入口共用它：两份规则一定会漂移，而漂移的后果是「同一个库对两个入口收的东西不一样」。
被拒的请求在这里就停下——不落盘、不建行、不投递，调用方换文件或换库即可。

落盘路径只由 ``document_storage`` 按服务端规则拼接，原始文件名只进
``source_filename`` 列，绝不进路径。

应用日志只放 ID 与派生量，不放检索文本原文、文档内容或 API Key。

跨租户访问与知识库不存在都抛 ``KNOWLEDGE_BASE_NOT_FOUND``，由 HTTP 入口适配器统一
翻译成 20008「不存在」——不返回「无权限」，避免泄露资源存在性。
"""

from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ErrorCode
from app.db.transaction import after_commit
from app.models.document import Document
from app.models.knowledge_base import FORMAT_MARKDOWN, KnowledgeBase, formats_of
from app.models.plan import PlanSpec
from app.repositories import document_repository
from app.services import static_quota
from app.services.document_storage import source_type_of, store_document_file
from app.services.index_dispatch import enqueue_document
from app.services.knowledge_base_access import ensure_visible
from app.utils.id_generator import new_document_id
from app.utils.status import DocumentStatus

logger = structlog.get_logger(__name__)


def admit_upload(kb: KnowledgeBase, *, filename: str | None) -> str:
    """准入判定的**唯一**实现：返回本次上传的 ``source_type``，不被收就抛 20028。

    ``filename is None`` 是 JSON 入口：它收的本来就是 markdown 风格文本，一律算
    ``markdown``（``text`` 从此只由 ``.txt`` 产生），这样用户手写的规范 markdown
    才能进 ``structure`` 库。文件入口按扩展名映射。

    判定就是「本次解析出的 ``source_type`` 在不在该库的 ``allowed_formats`` 里」，
    所以 JSON 与文件两个入口不会各有一套规则。错误信息带上该库允许的格式列表，
    调用方不需要再查一次库配置就能改成对的格式。
    """

    source_type = FORMAT_MARKDOWN if filename is None else source_type_of(filename)
    allowed = formats_of(kb)
    if source_type not in allowed:
        raise AppError(
            ErrorCode.UNSUPPORTED_DOCUMENT_FORMAT,
            f"该知识库不接受 {source_type} 格式，允许的格式：{'、'.join(allowed)}",
            data={"source_type": source_type, "allowed_formats": allowed},
        )
    return source_type


async def upload(
    db: AsyncSession,
    *,
    tenant_id: str,
    kb_id: str,
    plan: PlanSpec,
    title: str | None = None,
    content: str = "",
    filename: str | None = None,
    data: bytes = b"",
    request_id: str | None = None,
) -> dict:
    """登记一份文档并投递索引任务，返回可直接回给调用方的摘要。

    两种模式由入口按 content-type 决定：``filename`` 非空 = 文件模式（正文留空，
    Worker 从落盘文件解析）；否则 JSON 模式（``content`` 直接落库）。

    准入在**分配 ID 与落盘之前**判定：被拒的请求不留下存储文件、文档记录或索引任务。
    静态配额（单库文档数与并发处理数）紧跟在准入之后、**同样在落行与入队之前**：
    被拒的上传既不该占存储，也不该占一个 Worker 名额。

    会话类型是 ``AsyncSession``：数据面上传（第 11 步票 #132）与控制面上传（票 #130）
    都已经是异步栈，两边**必须是同一份实现**——控制面另写一套是这条链路历史上最贵的错
    （见 ``app/services/console_kb_service.py`` 模块文档）。
    """

    kb = await ensure_visible(db, tenant_id=tenant_id, kb_id=kb_id)
    source_type = admit_upload(kb, filename=filename)

    # 两条静态配额在这里一起判：上传是唯一会同时撞上「单库文档数」与「并发处理数」的
    # 动作，绑成一个函数之后，第二个上传入口很难只接一半。
    await static_quota.ensure_upload_quota(
        db, tenant_id=tenant_id, kb_id=kb_id, plan=plan
    )

    document_id = new_document_id()

    if filename is not None:
        source_file_path, source_filename = store_document_file(
            tenant_id=tenant_id, document_id=document_id, filename=filename, data=data
        )
        doc = Document(
            id=document_id,
            tenant_id=tenant_id,
            kb_id=kb_id,
            title=title or Path(filename).stem,
            content="",
            source_type=source_type,
            source_file_path=source_file_path,
            source_filename=source_filename,
            status=DocumentStatus.PROCESSING,
        )
    else:
        doc = Document(
            id=document_id,
            tenant_id=tenant_id,
            kb_id=kb_id,
            title=title or "",
            content=content,
            source_type=source_type,
            status=DocumentStatus.PROCESSING,
        )

    await document_repository.create_async(db, doc)
    # 投递排在提交**之后**（after_commit），不是「先 commit 再投递」：登记与它可能带着
    # 的审计行属于同一笔请求事务，提交之前投递会让 Worker 去读一行还没落库的文档。
    # 顺序上仍然保证「行先落地、任务后出发」，Worker 崩溃时这张行也还在，留待租约过期
    # 后的重试 / 重建。
    after_commit(
        db,
        lambda: enqueue_document(
            document_id=document_id,
            tenant_id=tenant_id,
            kb_id=kb_id,
            request_id=request_id,
            reparse=False,
        ),
    )

    logger.info(
        "documents accepted",
        document_id=document_id,
        tenant_id=tenant_id,
        kb_id=kb_id,
        source_type=source_type,
    )
    return {
        "document_id": document_id,
        "kb_id": kb_id,
        "status": DocumentStatus.PROCESSING,
        "chunk_count": 0,
        "error_message": None,
        "source_type": source_type,
    }
