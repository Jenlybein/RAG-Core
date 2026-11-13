"""Celery 应用：Worker 与 Web 进程只通过 Redis 队列与数据库状态字段通信。

``task_acks_late=True`` 的语义是「任务至少执行一次」：Worker 在任务**执行之后**才
确认，所以进程崩溃 / 机器重启时任务会被重新投递。正因如此，索引任务必须自己幂等
——数据库里的 CAS（``document_repository.try_claim_for_indexing``）就是为此存在的：
重复投递里的后来者抢不到索引权，直接跳过。这两者是**一对**，永远不要只删掉其中
一个。

``worker_prefetch_multiplier=1``：长任务每个 Worker 进程最多预取一条，避免一个
进程囤着一队任务、其他进程空转（embedding 是分钟级的长活）。
``task_track_started=True``：状态可观测（STARTED），排障时能区分「排队」与「在跑」。
``broker_connection_retry_on_startup=True``：容器里 Worker 常比 Redis 先起来，
首次连接失败要重试而不是直接退出。

Worker 启动入口：
``celery -A app.celery_app:celery_app worker --loglevel=info``。显式写上
``:celery_app`` 属性后缀是最稳的写法——应用变量不叫 ``app``；只写
``-A app.celery_app`` 虽然能靠 Celery ``find_app`` 扫描模块属性碰巧猜中，但显式后缀
不依赖那套猜测。

``include=["app.tasks.indexing", "app.tasks.side_evidence"]`` 是 Worker 能认出任务的前提：Worker 从本模块启动
时只会 import 这里显式列出的模块；缺了它，``enqueue_document`` 投出去的任务会被
当作 "Received unregistered task" 消费掉，整条索引流水线静默不跑。

**Worker 子进程的事件循环在这里起、也在这里收**（第 11 步票 #132 / ADR-0023）：

* ``worker_process_init`` → :func:`app.db.worker_loop.get_worker_loop`：让「起不来」暴露在
  **启动阶段**，而不是第一个索引任务上；
* ``worker_process_shutdown`` → :func:`app.db.worker_loop.shutdown_worker_loop`：在循环
  **还活着的时候** ``dispose`` 异步引擎、关掉该循环名下的 HTTP 客户端，然后才停循环。

没有这两个信号时连接只能等进程退出被 GC 丢掉（ADR-0023 的探针实测：服务端会话还开着、
池槽位永久 checked out）。``app/db/worker_loop.py`` 另有 ``atexit`` 兜底，但 prefork 子进程
可能走 ``os._exit``，所以**信号才是主路径**。
"""

from celery import Celery
from celery.signals import worker_process_init, worker_process_shutdown

from app.core.config import settings
from app.db.worker_loop import get_worker_loop, shutdown_worker_loop
from app.utils import tokenizer

celery_app = Celery(
    "rag_center",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    # 没有 include，Worker 进程永远不会 import 任务模块：enqueue_document 投出去的
    # 任务会被当成 "Received unregistered task" 直接消费掉，索引流水线静默不跑。
    include=["app.tasks.indexing", "app.tasks.side_evidence", "app.tasks.index_build"],
)
celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    broker_connection_retry_on_startup=True,
)


@worker_process_init.connect
def _verify_tokenizer(**_kwargs: object) -> None:
    """Worker 是真正做切块的进程：词表不可用就连子进程都不该起来。

    API 侧由 `app/main.py` 的 lifespan 校验过，但 Worker 不走 FastAPI 生命周期——
    没有这一处，词表缺失会拖到「第一次索引失败」才暴露，而那时错误信息指向的是
    「文档处理失败」，不是「部署缺资产」。
    """

    tokenizer.verify_ready()


@worker_process_init.connect
def _start_worker_loop(**_kwargs: object) -> None:
    """起常驻事件循环（ADR-0023 决策二）：异步连接池跟着它活，任务之间复用。

    在这里**真的把线程起起来**（``start()`` 而不只是拿到单例）：线程起不来、循环不可用
    属于部署问题，应当在 Worker 启动阶段就让子进程失败，而不是让第一条任务各自撞一次
    超时。任务体里的 ``run()`` 也会惰性调一次 ``start()``（幂等），所以这条只是把失败提前。
    """

    get_worker_loop().start()


@worker_process_shutdown.connect
def _stop_worker_loop(**_kwargs: object) -> None:
    """确定性释放：在循环还活着的时候 ``dispose`` 引擎、关该循环名下的 HTTP 客户端。

    重复关停是幂等的；每一步失败只记 warning——收尾阶段抛异常会盖掉真正的退出原因。
    """

    shutdown_worker_loop()
