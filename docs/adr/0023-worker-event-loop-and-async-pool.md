# 0023. Worker 进程的事件循环归属：一个进程一个常驻循环，异步连接池跟着它活

- 状态：**已采纳**（探针与决策在 #131；接线与验收在 #132 完成，落地清单一节见文末「接线落地」）
- 日期：2026-11-13
- 相关：父 spec #122「第 11 步」、票 #131（探针）、#132（接线）、**ADR-0009「异步索引用 Celery + `asyncio.run`」**（本文修订它「每任务一个循环只是丑、不是不安全」那句）、ADR-0022「连接复用按事件循环隔离」、`GLOSSARY.md`「Worker 常驻循环」「检索执行池」「索引链路」、`app/db/async_session.py`、`app/db/worker_loop.py`、`app/tasks/indexing.py`、`app/celery_app.py`、`docs/架构.md`「双栈期的数据库连接预算」、`tests/test_worker_loop.py`、`tests/test_integration_worker_loop.py`

## 一句话决定

Worker 每个子进程**一个常驻事件循环**（`app/db/worker_loop.py` 的 `WorkerLoop`），异步引擎的连接池跟着这个循环活：任务用**工厂函数**投进去、连接跨任务复用；退出时在**这个循环还活着的时候** `dispose()`。**不采用**「每任务一个 `NullPool` 引擎」。

## 背景：为什么这件事要单独探一次

第 11 步要把索引链路从同步 `Session` 换成 `AsyncSession`（#132）。改造前 ADR-0009 明确写过「每任务 `asyncio.run` 一次只是丑，不是不安全」，并且给了理由：数据库是同步 `Session`（不绑循环），异步 HTTP 客户端由 `app/core/http_clients.py` 按 loop 注册、每任务各自关。

换成异步引擎之后，那条理由的**前提**没了：异步引擎的连接池绑在**创建连接的那个事件循环**上，而 Worker 的循环是每任务一个、跑完即关。于是「连接归谁管」第一次成了一个需要决定的问题，而不是一句「反正每个任务自己关」。

## 探针：怎么证明的（不是「跑通了」）

三个方案各跑**两个任务**，量三件可观察的事：

1. **物理连接开了几条**——SQLAlchemy 的 `connect` 事件。这是「复用」的硬证据；
2. **后端 pid**（`select pg_backend_pid()`）——服务端确实是同一条会话；
3. **关停之后 `pg_stat_activity` 里还有没有这条会话**——这是「确定性释放」的硬证据。

用例：`tests/test_integration_worker_loop.py`（`RAG_INTEGRATION_DATABASE_URL` 门禁，与 `test_integration_cas.py` 同一条：默认套件整模块 skip）；机制的离线契约在 `tests/test_worker_loop.py`（假引擎，证明「同一个循环、引擎在那个循环上 dispose、线程与循环收干净」）。

**读数**（本机 PostgreSQL 16.15 / psycopg 3.2.3 / SQLAlchemy 2.0.36，每方案两个任务）：

| 方案 | 物理连接 | 后端 pid | 任务结束后池里 | 关停后服务端会话 | 每任务往返（本机） |
| --- | --- | --- | --- | --- | --- |
| **A 常驻循环**（本文选的） | **1** | 同一个 | 1（复用中） | **已结束** | **2.28 ms** |
| **B 任务级 `NullPool`** | 2 | 两个 | 0（不存连接） | 已结束 | 21.79 ms（≈9.6×） |
| **现状：模块级引擎 + 每任务 `asyncio.run`** | 1 | 同一个 | **1，没人关** | **还开着** | 2.3 ms |

三条要读对的地方：

1. **现状在 happy path 上是「成功」的。** psycopg 3.2 的异步连接并不把 loop 记在自己身上，跨 loop 复用一条连接查询**能跑通**——所以「这么写会当场炸」在这条栈上**不成立**。`Event loop is closed` 是 httpx 的症状（ADR-0022），数据库这边没有。危险恰恰在这里：**错法是沉默的**。
2. **代价落在关停。** 两个任务的循环都关了，池里那条连接还在、服务端会话也还开着，任务路径里没有任何一步能在**活着的**循环上 `dispose()`。探针必须自己再造一个循环才关得掉它（`test_the_status_quo_leaves_a_connection_nothing_in_the_task_path_can_close`）。进程退出时那条连接只能被 GC 丢掉（实测 `ResourceWarning: connection <psycopg.AsyncConnection [IDLE]>`）。
3. **B 是「正确但贵」**：每任务重新握手（本机 21.79 ms vs 2.28 ms；跨网络 + TLS + 认证只会更差）。索引任务动辄分钟级，9.6× 本身不算致命——真正的代价是**连接预算与可观测性**：`NullPool` 下「池」不再是一个能读的数字，`pg_stat_activity` 里本库连接数随任务抖动，而 `docs/架构.md` 的连接预算算式是**按每进程一个池**写的。

两条附带实测（不属于本票的选择，但 #132 必须知道）：

- **被放弃的 checked-out 连接，A 也救不回来**：实测 `dispose()` 之后它仍然停在 `idle in transaction`（占着快照与锁），只能 `pg_terminate_backend`。所以「每任务 `finally` 里关会话」不能因为换了循环就省掉；`pool_pre_ping=True` 只能保证**下一次**借到的是好连接。
- **Windows 上 psycopg 异步不支持缺省的 `ProactorEventLoop`**：`asyncio.run(...)` 的第一条查询就抛 `InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async mode`。`new_worker_loop()` 因此在 win32 上显式造 `SelectorEventLoop`。ADR-0009 本来就规定 Worker 只跑 Linux 容器，所以这一支只服务本机探针与脚本——但没有它，本机连探针都跑不起来，而 CI 是绿的。

## 决策一：一个进程一个常驻循环（A），不采用任务级 `NullPool`（B）

**考虑过的替代方案**：

| 方案 | 为什么不选 |
| --- | --- |
| 现状：模块级引擎 + 每任务 `asyncio.run`（任务里不 dispose） | 连接由最后一个任务的循环持有，而那个循环已经关闭：任务路径里没有活着的循环可以 `dispose()`，连接只能等 GC 或进程退出（实测 `ResourceWarning`）。被取消 / 中断的任务还会把池里的槽位永久标成 checked out（实测 `Checked out connections: 1`），`pool_size` 个之后下一个任务卡在 `pool_timeout`（缺省 30 秒）上超时——症状是「Worker 偶尔一条任务超时，重启就好」。 |
| 任务级 `NullPool`（B） | **正确**（每任务建毁、无残留，探针也证明了），但每任务一次完整握手；更麻烦的是它把「每进程一个池」这条预算前提拆掉了：连接上限变成一个随任务浮动的数，`pg_stat_activity` 里本库连接数不再可预期。索引任务是分钟级长活，省下的那点复杂度换来的是每次任务多一次建连。 |
| 把索引任务塞进 `app/core/loop_pool.py` 的检索执行池 | 那个池是为「同步查询会卡住循环」的向量分支准备的，固定 4 条循环、排队预算 2 秒；索引任务要的是「一条循环 + 分钟级预算」。塞进去等于连接预算乘 4、语义也错位。`GLOSSARY.md` 里本来就写着「索引链路与 Celery 不进这个池」。 |
| 每任务新建引擎但保留 `QueuePool` | 等于 B 的代价，还多一条「池永远留不下连接」的浪费：每任务一个池，任务结束池就没人管了。 |
| 换成 asyncpg 驱动 | ADR-0022 已把「换驱动」留作翻案条件；换驱动不改变「池绑循环」这件事，本文的结论不因它而变。 |
| 让 Celery 换成 async 框架（arq 等） | ADR-0009 已经否掉（maintenance-only），本票不改这个前提。 |

**选择的理由**：

- **复用是确定的**：两个任务一条物理连接（`connect` 事件 1 次，pid 相同），而不是每任务一条。
- **释放是确定的**：`shutdown()` 在循环还活着的时候 dispose 引擎、关它名下的 HTTP 客户端，然后才停循环——这正是现状做不到的那一步。
- **连接预算不用重算**：一个进程一套池，`docs/架构.md` 的算式（同步 15 + 异步 15 每进程）继续成立；换成 B 反而要把那一节改成「按任务浮动」。
- **接线面小**：#132 只需要两个信号各一行（见决策二），任务体把 `asyncio.run(...)` 换成 `get_worker_loop().run(...)`。
- **并发由进程数给，不由循环数给**：Celery prefork 每个子进程同一时刻只跑一个任务，多开循环只会把连接池变成 N 份。

**代价**：

- Worker 进程多一条常驻线程与一个常驻循环；`worker_process_shutdown` 没接上时靠 `atexit` 兜底（prefork 子进程可能走 `os._exit`，所以**信号才是主路径**）。
- **循环一旦常驻，任务体里就不能再关当前循环的 HTTP 客户端**。`app/tasks/indexing.py` 的 `_index_in_its_own_loop` 现在在 `finally` 里 `await aclose_current_loop_clients()`——那是为「每任务一个循环」写的；接线之后它等于每任务把共享客户端关掉，复用立刻归零。收尾必须上移到 Worker 退出（`WorkerLoop.shutdown` 已经做了）。
- 「每任务一个干净循环」这个心智模型作废：任何把协程 / 客户端 / 连接挂在模块级并指望「任务结束就没了」的写法都会变成跨任务共享。ADR-0022 的「按 loop 注册」继续兜住这一类。
- 一个任务卡在同步调用里会挡住同一进程的后续任务（prefork 本来就是这样），但循环被卡住时 `shutdown` 的 dispose 可能超时，只记 warning。

## 决策二：Worker 退出时的确定性释放

`WorkerLoop.shutdown()` 的顺序（每一步失败只记 warning——收尾阶段抛异常会盖掉真正的退出原因）：

1. 停止接收（之后的 `run` 立刻 `RuntimeError`，不排队、不挂死）；
2. **在这个循环上** `await engine.dispose()`（连接在这里才关得掉）；
3. 在这个循环上 `aclose_current_loop_clients()`（它名下的 HTTP 客户端）；
4. 停循环 → join 线程 → 线程的 `finally` 里 `loop.close()`。

**接线要求（#132 做，本票刻意不做）**：

```python
@worker_process_init.connect
def _start_worker_loop(**_kwargs): get_worker_loop().start()   # 起不来就在启动阶段暴露

@worker_process_shutdown.connect
def _stop_worker_loop(**_kwargs): shutdown_worker_loop()      # 确定性释放
```

`start()` 而不是只拿单例：线程要**真的起来**，「起不来」才会在启动阶段炸，而不是等第一条
任务撞超时（`run()` 里的惰性 `start()` 是幂等的，这条只是把失败提前）。

`app/db/worker_loop.py` 已经在模块级 `atexit.register(shutdown_worker_loop)`：没有信号、也没有 lifespan 的进程（脚本、排障、评测）靠它兜底。

**证据**：`test_the_resident_loop_reuses_one_physical_connection` 断言关停后 `engine.pool.status()` 里连接数为 0、`pg_stat_activity` 里那条会话已消失、且关停不产生连接噪音 warning；`tests/test_worker_loop.py` 用假引擎断言 dispose 发生在**任务用的那个循环**上、线程与循环都收掉、重复关停幂等、关停后再投任务立刻报错。

## 决策三：引擎仍然是进程级那一个（`app/db/async_session.py:async_engine`）

`WorkerLoop.engine()` 缺省返回进程级引擎，而不是另建一个：一个进程一套池，连接预算的算式继续成立，`AsyncSessionLocal` 也不用改指向（它本来就绑在这个引擎上）。引擎对象本身在任何线程都能取到（惰性连接），**绑循环的是连接**——所以第一次 `connect` 必须发生在 `run` 里面，这也是「任务用工厂函数投递」的另一半理由。

## 不这么做会怎样（汇总）

- **连接没人关**：任务路径里没有活着的循环可以 `dispose()`，连接留到 GC 或进程退出。GC 只能丢（`ResourceWarning`）；被放弃的 checked-out 连接连丢都丢不干净（`SAWarning: ... cannot be safely terminated`），服务端那条停在 `idle in transaction`，占着快照与锁。
- **池的账不可信**：`pool.checkedout()` 会永久停在 1（实测），`pool_size` 个之后下一个任务卡在 `pool_timeout`（缺省 30 秒）上超时。排障时看到的是「偶尔超时 + 重启就好」，而 `pg_stat_activity` 里躺着几条 `idle in transaction`——两条线索都不指向「事件循环」。
- **本机（Windows）连探针都跑不起来**：ProactorEventLoop + psycopg 异步 = 第一条查询就 `InterfaceError`。

## 翻案条件

- **索引任务变成一个任务内高并发多文档**：那时单个常驻循环就是并发上限，需要按 loop 数的池——应当复用 `app/core/loop_pool.py` 的形状，而不是再发明一个。
- **换成 asyncpg 之后**跨 loop 复用的隐患换了形状（asyncpg 的连接确实记着自己的 loop）：那时重新评估「每任务 `NullPool` + 预热」是否反而更简单。
- **Celery 换成 async 框架**（arq / dramatiq async）：循环本来就是框架的，`WorkerLoop` 退化成薄封装，本文的决策二可以删掉。

## 接着验证

```bash
# 机制（离线，假引擎）：同一个循环、引擎在那个循环上 dispose、线程与循环收干净
uv run pytest tests/test_worker_loop.py -q

# 真实连接复用 + 确定性释放（需要真库；与 test_integration_cas.py 同一条门禁）
RAG_INTEGRATION_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/rag_test \
  uv run pytest tests/test_integration_worker_loop.py -q
```

对照阅读：

- `app/db/worker_loop.py`：应能看到 `run()` 用 `run_coroutine_threadsafe` 投**工厂函数**、`shutdown()` 的四步顺序、win32 的 `SelectorEventLoop` 分支；
- `tests/test_integration_worker_loop.py`：应能看到三条用例分别对应 A / B / 现状，加一条并发连接上限（票 #132 加的），且断言的是 `connect` 事件数、后端 pid、`pg_stat_activity` 与 `pool.checkedout()`，不是「跑通了」；
- `app/tasks/indexing.py`：**#132 起已接线**——任务体把工厂投进 `get_worker_loop().run(...)`，那句 `finally: await aclose_current_loop_clients()` 已经删掉（见下节）。

## 接线落地（#132）

接线本身只有三处，加上一处**必须删掉的收尾**：

| 位置 | 接线 | 为什么是这里 |
| --- | --- | --- |
| `app/celery_app.py` | `worker_process_init` → `get_worker_loop().start()`；`worker_process_shutdown` → `shutdown_worker_loop()` | 让「起不来」暴露在启动阶段（线程要**真的**起来）；让 `dispose` 发生在**循环还活着**的时候。`atexit` 只是兜底（prefork 子进程可能走 `os._exit`）。 |
| `app/tasks/indexing.py` | `get_worker_loop().run(lambda: _index_in_the_worker_loop(...))`——投**工厂**，协程与连接从这一刻起属于常驻循环 | 投协程对象等于在任务线程里把协程绑到另一个循环上；工厂让「第一次 `connect` 发生在 `run` 里面」（决策三）。 |
| `app/tasks/indexing.py` | 任务会话 `async_session_factory()()` 在**循环里**开、在 `finally` 里 `await close()` | 复用的是**连接池**（归循环），不是会话（归任务）。关会话不能省：被放弃的 checked-out 连接连 `dispose` 都救不回来（会停在 `idle in transaction`）。 |
| `app/tasks/indexing.py` | **删掉** `_index_in_its_own_loop` 的 `finally: await aclose_current_loop_clients()` | 那句是为「每任务一个循环」写的；接线之后它等于每个任务把共享客户端关掉，复用立刻归零。收尾上移到 `WorkerLoop.shutdown`。 |

**证据**（都在默认套件里，除最后两条走 `RAG_INTEGRATION_DATABASE_URL` 门禁）：

- `tests/test_http_clients.py::test_the_worker_task_keeps_the_clients_it_built_across_tasks`：两个任务共用一个客户端，任务结束时**不关**，`shutdown_worker_loop()` 才关；
- `tests/test_indexing_task.py`：任务体跑在**真的** `WorkerLoop` 上（只有会话工厂与仓储是替身），重试 / 失败留痕 / `request_id` 关联照旧；
- `tests/test_document_pipeline_e2e.py`：上传 → 投递（真的常驻循环）→ 解析 → 切块 → 写入 → SUCCESS 一条链；
- `tests/test_integration_worker_loop.py::test_concurrent_tasks_stay_under_the_pool_ceiling_and_return_every_connection`：24 个并发任务、池上限 15，物理连接不超过上限、跑完 `checkedout() == 0`——「连接风暴」那一条的硬证据；
- `tests/test_integration_cas.py` 的异步孪生两条：CAS 与重试生命周期在 `AsyncSession` 上同样成立。

**观测**：`WorkerLoop.shutdown()` 在 `dispose` 之前记一行 `worker_loop_pool_state`（`pool_size` / `checked_out`）。上限是配置，读数是这一行；泄漏的样子就是关停时 `checked_out` 还不为 0。

**自检**：一个没读过本项目的人，能否只看这份 ADR 说清「为什么不能每个任务 `asyncio.run` 一次就完事」「为什么没选 `NullPool`」「Worker 退出时连接是谁关的、在哪一刻关」？说不清就补大白话，不要补术语。
