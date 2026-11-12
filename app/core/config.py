"""配置收口：所有环境变量只在这里读一次。

骨架期之后，第 3 步（文档处理链路）在这里一次补齐 11 项：Celery/Redis、
文档入库、embedding 批次与 OCR 运行环境。.env 模板里还有更多阶段变量，
pydantic-settings 默认忽略未声明的键（extra="ignore"），不会因多配置报错。

第 4 步票 1 再补一组「模型接入」：供应商 + 模型 + per-provider key + 思考强度
（``LLM_REASONING_EFFORT``，只作用于 strong **模型角色**）。
它们全部是**运行时开关**而不是代码分支：换一家供应商只改 .env，不动业务代码；
一家都没配时工厂返回空实现，离线环境照常启动。

``PDF_INSPECTOR_MODEL_CACHE`` 由 pdf-inspector 库自己读取，因此不在这里声明；
它只在 Dockerfile / CI 里作为环境变量设置。
"""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.llm_vendors import LLM_VENDORS

# 检索模式的允许值。放在模块级而不是写成类属性：它是一份**词汇表**，校验器与
# 报错文案共用同一份，不会出现「文案说支持 A、校验器只放 B」。票 4 把 hybrid 加进来，
# 消费方那边的 `Literal`（app/services/retrieval_options.py 的 `RetrievalMode`）必须
# 同步加——`Literal` 没法从元组展开，两处写两遍是语言限制，用
# tests/test_hybrid_retrieval.py 的一致性测试钉住，防漂移。
# 第 7 步（#77）起这个值只是**缺省值**：请求体里没有 ``mode`` 字段，实际取值来自
# 密钥配置的解析（app/services/retrieval_options.py）。
SUPPORTED_RETRIEVAL_MODES = ("vector", "bm25", "hybrid")

# 观测去向的三个取值（#102）。与检索模式同一个理由放在模块级：校验器与报错文案共用
# 一份，不会出现「文案说支持 A、校验器只放 B」。三个实现见 ``app/observability/``。
# 名字按票面写 ``none``：ADR-0021 里那个 ``null`` 指的是同一个去向（丢弃），
# 只是当时用的是 SQL / 语料里的叫法，落地时统一成这个。
SUPPORTED_TRACE_SINKS = ("none", "jsonl", "langfuse")


class Settings(BaseSettings):
    """从 .env 加载的运行时配置。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_ENV: str = "local"
    DATABASE_URL: str = "postgresql+psycopg://postgres:123456@localhost:5432/rag_db"
    VECTOR_STORE: str = "pgvector"

    MODEL_BASE_URL: str = ""
    MODEL_API_KEY: str = ""
    EMBEDDING_MODEL: str = "qwen3.7-text-embedding"
    EMBEDDING_DIMENSIONS: int = 1536
    # 百炼 embedding 单批上限按模型不同：text-embedding-v3/v4 = 10、
    # v1/v2 = 25、qwen3.7 = 20；默认取最保守的 10。
    EMBEDDING_BATCH_SIZE: int = 10

    # 切块窗口的**长度单位是 Qwen token**（与 embedding 模型同一套分词），不是字符。
    # 512 token ≈ 768–983 汉字，50 token ≈ 75–95 汉字（约 10%）。
    # 重叠从 100 降到 50：边界已由分隔符保证，大重叠只在「硬切」时代才有必要。
    CHUNK_SIZE: int = 512
    CHUNK_OVERLAP: int = 50
    # Qwen 词表路径；留空则用仓库根的 .tokenizer/qwen.tiktoken。
    # 它是**必需资产**：缺失时应用拒绝启动（见 app/utils/tokenizer.py）。
    QWEN_TOKENIZER_PATH: str = ""
    TOP_K: int = 5

    # 文档入库：原始文件落盘目录、上传大小上限、单个表格切片的最大行数。
    DOCUMENT_STORAGE_PATH: str = "./data/uploads"
    DOCUMENT_MAX_SIZE_MB: int = 20
    TABLE_MAX_ROWS_PER_CHUNK: int = 10

    # 异步（第 3 步）：Celery 走 Redis，broker 与 result backend 分库。
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/1"

    # OCR（第 3 步）：off / auto / force；离线模式与模型目录主要给容器与 CI 用。
    # PDF_INSPECTOR_MODEL_CACHE 由 pdf-inspector 库自己读取，不在这里声明，
    # 只在 Dockerfile / CI 里设置。
    OCR_MODE: str = "auto"
    OCR_OFFLINE: bool = False
    OCR_MODEL_DIRECTORY: str = ""
    PDFIUM_LIB_PATH: str = ""
    ORT_DYLIB_PATH: str = ""

    # 结构化日志（T2）：只新增这两项，滚动阈值/份数是 setup_logging 的参数。
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "logs"

    # 占位 Key（第 1 步）：数据面两个入口共用。认这个固定 token，
    # tenant_id 取默认租户、不做真实校验；第 7 步换成 api_keys 表真校验。
    DEV_API_KEY: str = "dev-key"
    DEV_TENANT_ID: str = "tenant_demo"
    # 鉴权开关（第 7 步）：关闭时落进内置开发身份，但**仍然要求**带上上面那把占位 token
    # ——退回「谁都不用带」意味着一个被意外暴露的开发实例对整个网络敞开，而要求这把固定
    # token 对开发者零成本（它本来就在 .env 里），还让既有测试保持有意义。
    # **生产必须打开**：打开后只认 api_keys 表里的真 Key，开发身份不可达。
    AUTH_ENABLED: bool = False
    # 限流与日配额开关（第 7 步票 #79）：**默认关闭**，与上面那个开关同一个精神——
    # 本地开发与默认测试套件全程离线，一条真实 Redis 都不需要，既有用例因此一行不改。
    # 关掉时两道关卡（秒级限流、按天配额）整段是 no-op：**所有租户都是无限额度**。
    # **生产必须打开**：这是部署方的决定，不是代码能替它猜的。
    # 刻意做成一个显式开关，而不是「连不上 Redis 时自动放行」——Redis 一挂就让所有人
    # 解除限流，是本票最坏的失败模式：平台看起来完全健康（每次调用都成功），直到账单
    # 或 Worker 队列把真相捅出来。「现在到底有没有在限流」必须有人配过，而不是网络
    # 抖动的副产品。
    RATE_LIMIT_ENABLED: bool = False
    # 套餐缓存（第 7 步）：与 Celery 的 0/1 号库分开，避免哪天有人在那两个库上 FLUSHDB
    # 顺手把套餐缓存清空（或反过来）。读走缓存、改完删缓存，TTL 只是兜底。
    REDIS_URL: str = "redis://localhost:6379/2"
    PLAN_CACHE_TTL_SECONDS: int = 60
    # 控制台会话（第 7 步控制面）：有效期只有一个来源——登录时发 cookie 与写会话行的
    # ``expires_at`` 读的是同一个值，两处各写一遍迟早出现「cookie 还活着、行已过期」。
    # 停用账号不靠它生效（那是每个请求回账号表现读），它只回答「多久不用就自动掉线」。
    CONSOLE_SESSION_TTL_SECONDS: int = 28800
    # 离线测试 / CI 不启 MCP 会话管理器（避免每个 TestClient 都启停 session manager）。
    # 生产默认开启；仅 pytest 通过环境变量关掉。
    ENABLE_MCP: bool = True

    # ── 模型接入（第 4 步票 1）──
    # 标识是「供应商 + 模型」，key 按供应商各存一份：换一家只改环境变量，业务代码零改动。
    # 这里**每一项都必须有默认值**——tests/test_contracts.py 用 Settings(_env_file=None)
    # 构造配置对象，任何必填项都会当场炸。
    LLM_PROVIDER: str = "deepseek"
    # 留空 = 用该供应商的默认地址（千问例外：工作空间 ID 嵌在 URL 里，必须自己填）。
    LLM_BASE_URL: str = ""
    LLM_MODEL: str = "deepseek-flash"
    LLM_STRONG_MODEL: str = "deepseek-v4-pro"
    LLM_TIMEOUT_SECONDS: float = 60.0
    # 思考强度只作用于 strong 模型角色；fast 模型角色在 Provider 里写死关闭
    # （改写与重排要低延迟）。
    LLM_REASONING_EFFORT: str = "off"

    # 各供应商的 key。哪个留空，就等于「这台机器没配这家」——工厂据此退回空实现。
    # 注意 OPENAI_API_KEY 等名字与各家 SDK 自己的环境变量同名：这是刻意的，
    # 运维不用记两套名字。
    DEEPSEEK_API_KEY: str = ""
    QWEN_API_KEY: str = ""
    ARK_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    GEMINI_API_KEY: str = ""

    # 嵌入实现开关：langchain（框架抽象，默认）/ openai_compatible（原有直连实现，回退）。
    EMBEDDING_PROVIDER: str = "langchain"

    # ── 重排（第 4 步票 7；第 10 步票 3 换成专用端点）──
    # 默认关闭：关闭时工厂返回空实现，默认路径**一次模型都不调**——离线测试、CI、
    # 没配 key 的机器都靠这一条保持全绿。
    RERANK_ENABLED: bool = False
    # 专用 Cross-Encoder 端点（OpenAI 兼容 ``POST {base}/reranks``）。留空 = 这台机器
    # **没有**重排端点：``RERANK_ENABLED=true`` 也只会拿到空实现（明确不可用），
    # 不再回落「让通用大模型逐条打分」——那个实现还在（评测要拿它做对照），但生产工厂
    # 不再返回它。
    #
    # ⚠️ 路径段与 embedding 不同：嵌入是 ``…/compatible-mode/v1``，重排是
    # ``…/compatible-api/v1``。两者不能互相顶替，必须单独配（见 .env.example）。
    RERANK_BASE_URL: str = ""
    RERANK_MODEL: str = "qwen3.7-text-rerank"
    # 重排端点自己的 key；留空则回落 ``MODEL_API_KEY``（同一把百炼 key 通常两处都通）。
    RERANK_API_KEY: str = ""
    # 送进模型的候选上限与单条切片截断长度：提示词长度就是延迟与成本。
    # 候选数另有一层硬上限 50（``app/providers/rerank/qwen.py``），配置写多大都越不过去。
    RERANK_MAX_CANDIDATES: int = 20
    RERANK_CHUNK_MAX_CHARS: int = 1024
    # 超时由重排 Provider 用 ``asyncio.wait_for`` 兜住：慢调用按失败处理，走降级。
    RERANK_TIMEOUT_SECONDS: float = 15.0
    # 注：**返回条数只由这次调用解析出的 ``top_k`` 决定**（第 7 步起它来自密钥配置，
    # 不再来自请求体），没有「重排条数上限」这一项。
    # 服务端配置悄悄把调用方要的 10 条压成 5 条，是意外行为而不是配额。
    # 原先的 ``RERANK_TOP_N`` 与 ``RERANK_TEMPERATURE`` 已删除：前者压条数，后者
    # 从没接进模型（``LLMProvider`` 不承载生成参数，重排温度在装配层固定为 0）。

    # ── 查询优化（第 4 步票 6）──
    # 两层各自一个开关，**都默认关闭**：关掉时这一步是恒等变换，检索行为与票 4
    # 逐字段一致；离线测试 / CI / 没配 key 的机器也靠这两条保持「一次模型都不调」。
    QUERY_REWRITE_ENABLED: bool = False
    # 改写的超时（毫秒）。超时按失败处理 → 回退原句并标降级。它必须小于客户端超时：
    # 一个慢调用把整次检索拖到客户端先放弃，比降级更糟。
    QUERY_REWRITE_TIMEOUT_MS: int = 2000
    # 同义词扩展是纯函数，不需要模型也不需要超时；开与不开只影响关键词一路。
    SYNONYM_EXPANSION_ENABLED: bool = False

    # ── 节说明（检索文本补一句上下文）──
    # 索引期为**每个节**生成一句「本节在讲什么」，拼进检索文本。按节生成而不是按片：
    # 456 片就是 456 次调用，成本与延迟都不可接受；节级说明已经能表达「这片属于什么场景」。
    # 默认关闭：关掉时检索文本与现状逐字一致，离线测试也保持「一次模型都不调」。
    SECTION_CONTEXT_ENABLED: bool = False
    SECTION_CONTEXT_TIMEOUT_MS: int = 5000
    # 送给模型的节正文最多这么长：节很长时全部塞进去既慢又贵，说明只需要开头。
    SECTION_CONTEXT_MAX_INPUT_CHARS: int = 1200

    # 框架自带的追踪客户端读的是环境变量而不是这份配置。这里声明默认值，
    # 由 Provider 工厂在构造模型前同步到进程环境里（见 providers/llm/langchain_llm.py）。
    LANGCHAIN_TRACING_V2: bool = False

    # ── 观测旁路（第 9 步，#102）──
    # 一个配置项切三个去向：观测服务（``langfuse``）/ 本地文件（``jsonl``）/ 丢弃（``none``）。
    # **切换不改链路代码**：链路只管「六段各花了多久」，送到哪由这里决定。
    #
    # 默认 ``jsonl`` 的理由要连起来读：**默认开启观测，但不依赖任何外部服务**。
    # 观测栈是自托管的一整套 five-container（ADR-0021 决策 4），没起栈的机器上选 ``langfuse``
    # 只会让每次检索都留一条 warning；而完全关掉（``none``）又等于默认部署里「哪一步慢」
    # 根本没有答案。``jsonl`` 是这个缺口里唯一两全的选择：一行一次检索、本机可 grep。
    # ``none`` 是测试与 CI 的去向（见 ``tests/conftest.py``），不是生产缺省。
    #
    # 注意它**不是**「计时开关」：任何取值下六段耗时都会被算出（父 spec「度量常开」），
    # 这个值只决定这份计量送到哪。所以别把它接进任何 ``if`` 里省掉计时。
    TRACE_SINK: str = "jsonl"
    # JSONL 去向的目录与保留天数。保留天数有两处用处：写入时清掉超期文件、读取时只扫
    # 这个窗口（``app.observability.jsonl_sink.read_recent_records``）。单行 ≤ 2 KB 是
    # 契约常量，不在这里配（见 ``jsonl_sink.MAX_LINE_BYTES``）。
    TRACE_JSONL_DIR: str = "logs/traces"
    TRACE_JSONL_RETENTION_DAYS: int = 7
    # 自托管观测栈的连接信息。留空 = 这台机器没配：``langfuse`` 去向会在第一次用时失败，
    # 失败只留 warning（观测是旁路，ADR-0021）。名字与上游 SDK 的环境变量同名，是刻意的。
    LANGFUSE_HOST: str = "http://localhost:3000"
    # 浏览器该用哪个地址打开观测栈 UI。留空 = 与 ``LANGFUSE_HOST`` 相同。
    #
    # **为什么需要两个**：容器里 API 要按服务名找栈（``http://langfuse-web:3000``），而浏览器
    # 解析不了那个名字。控制台「在观测栈里打开」的链接因此不能直接用 ``LANGFUSE_HOST``——
    # 那会给出一个只有容器内能访问的地址。分出一个「对外地址」，两边各说各的那一份。
    LANGFUSE_PUBLIC_URL: str = ""
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""

    # ── 检索与关键词引擎（第 4 步票 3 / 票 4）──
    # 默认 hybrid：票 4 把融合与降级做完后翻回来（父 spec：缺省值取服务端配置）。
    # 混合模式下关键词一路不可用只会降级，不会让默认配置在离线机器上直接报错。
    RETRIEVAL_MODE: str = "hybrid"
    # 默认 none：离线测试、CI、没部署 ES 的机器照常启动，关键词能力明确不可用
    # （mode=bm25 时返回 RETRIEVAL_FAILED，绝不偷偷退回向量）。.env 模板里是
    # elasticsearch，本机起了容器就能用。
    KEYWORD_SEARCH_PROVIDER: str = "none"
    ELASTICSEARCH_URL: str = "http://localhost:9200"
    ELASTICSEARCH_INDEX: str = "rag_chunks"
    # 关键词这一路的**时间预算**（秒）：超过就当作「这一路挂了」，走既有降级。
    #
    # 为什么需要它（一次真实的 19 秒）：ES 容器被杀之后，每次检索都在等一条永远不会通的
    # 渠道——客户端自己的重试把 DNS 失败累计成 ~19 秒，而其它五段加起来不到 600ms。
    # 界面上表现为「检索突然变得极慢」，而真相是关键词渠道已经不在了。
    #
    # 关键词是**可降级**的一路（ADR-0021 同一口径：旁路挂了不该拖垮主路），所以这里给出
    # 一个显式预算，而不是把「等多久」交给客户端库的默认值与它内部的重试策略。
    # 健康集群上 BM25 查询是毫秒级，2 秒已经非常宽松。
    KEYWORD_SEARCH_TIMEOUT_S: float = 2.0
    # 旁证跑批的并发题数（#109）。**默认 4 而不是 1**。
    #
    # 实测（2026-11-11）：单次模型往返只有 1.5 秒，但 ragas 的 ContextPrecision /
    # ContextRecall **不是每题一次调用**——它把每个片段拆成陈述句逐句判相关性，粗算每题
    # 20~30 次请求。于是 50 题串行 ≈ 1000+ 次调用 ≈ 半小时，而 CPU 全程不到 1%
    # （纯等网络）。这里并发几题，等于把「等」叠起来，收益接近线性。
    #
    # 上限别开太大：额度与限流是共享的，4~8 已经能把半小时压到几分钟。
    SIDE_EVIDENCE_CONCURRENCY: int = 4
    # 混合模式的**每路召回深度**。「召回宽、返回窄」：两路各找这么多条再融合，
    # 最后仍只返回 ``top_k`` 条。它和 ``top_k`` 是两件事——池子只有 top_k 时，
    # 在 5+5 里挑 5 条，融合几乎无事可做，混合检索的收益会被候选池的宽度吃掉。
    # 服务端配置，不进请求体（质量参数一旦可调，同一档位就是不同链路）。
    HYBRID_RECALL_TOP_K: int = 20
    # 第 11 步票 #127 删掉了两个键：``RETRIEVAL_POOL_SIZE`` / ``RETRIEVAL_QUEUE_BUDGET_MS``。
    # 它们是固定规模检索执行池的旋钮，而池本身已经删除（向量分支回到普通 ``await``，
    # 见 ADR-0022「第五次修订」）。留着两个「改了也不生效」的键，比删掉更容易误导运维。

    @field_validator("RETRIEVAL_MODE")
    @classmethod
    def _validate_retrieval_mode(cls, value: str) -> str:
        """配错的检索模式必须**启动就炸**，不能等到每个请求各自 20002。

        这是真踩过的坑：请求体里的 ``mode`` 曾经有 ``Literal`` 守着，但**缺省值**来自
        这里，而 Pydantic 不校验 ``default_factory`` 的返回值。结果是配置写成
        ``bing`` 之类的拼错值时进程照常起来，之后每个请求都在业务层撞
        ``PARAM_ERROR``——排查成本比启动失败高一个数量级。第 7 步（#77）之后请求体里
        已经**没有** ``mode`` 字段，这个值成了唯一缺省，启动即校验因此更要紧。
        """

        allowed = SUPPORTED_RETRIEVAL_MODES
        normalized = value.strip().lower()
        if normalized not in allowed:
            raise ValueError(
                f"RETRIEVAL_MODE 只支持 {allowed}，当前是 {value!r}。"
            )
        return normalized

    @field_validator("TRACE_SINK")
    @classmethod
    def _validate_trace_sink(cls, value: str) -> str:
        """配错的观测去向必须**启动就炸**，理由与 ``RETRIEVAL_MODE`` / ``LLM_PROVIDER`` 同构。

        这个值决定「六段耗时送到哪」，而它的失败方式格外隐蔽：写成 ``json``（少一个 l）
        之后进程照常起来、检索照常返回，只是**一条 trace 都没有**——没有报错、没有告警，
        要等到有人翻观测栈时才发现这一整段时间是空白。启动失败比这便宜得多。
        """

        allowed = SUPPORTED_TRACE_SINKS
        normalized = value.strip().lower()
        if normalized not in allowed:
            raise ValueError(
                f"TRACE_SINK 只支持 {allowed}，当前是 {value!r}。"
            )
        return normalized

    @field_validator("LLM_PROVIDER")
    @classmethod
    def _validate_llm_provider(cls, value: str) -> str:
        """配错供应商名也必须**启动就炸**，理由与 ``RETRIEVAL_MODE`` 完全同构。

        这是真踩过的坑：用户本机的 ``.env`` 里写着 ``LLM_PROVIDER=openai_compatible``
        ——一个历史名字，工厂的装配表里没有它。修复前进程照常起来，配置看着「有值」，
        于是改写与重排永远跑不起来，直到**第一次调用模型**才炸成一次 500；从 API 日志
        倒推回一个环境变量，比启动失败难查一个数量级。

        名单来自 ``app.core.llm_vendors``：config 与工厂 import 的是同一份，这里
        **不复制厂商名**。两处真相的代价是「加一家只改一处、另一处静默不同步」，
        而这条链路的失败方式（模型调用 401 或 KeyError）都不会指向名单本身。
        """

        allowed = LLM_VENDORS
        normalized = value.strip().lower()
        if normalized not in allowed:
            raise ValueError(
                f"LLM_PROVIDER 只支持 {allowed}，当前是 {value!r}。"
            )
        return normalized


settings = Settings()
