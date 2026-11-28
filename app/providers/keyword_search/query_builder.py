"""关键词索引的三个纯函数：mapping、查询体、按文档删除的查询体。

**为什么单独一模块**：执行层（``elasticsearch.py``）只做「组装 + 发出去」，
所有值得断言的东西都落在返回的字典上。这里不 import 数据库会话、不构造任何
客户端、不发网络请求——离线测试直接对着字典断言即可，升级 ES 客户端也不会
把测试一起打碎。

四处最容易被写错的地方，都在这里钉死：

1. **过滤维度用 keyword、可检索文本用中文分词**。租户 / 库 / 版本 / 文档 /
   切片只做精确匹配，分词只会让它们变慢且匹配不到。
2. **写入与查询用两个分析器**。写入 ``ik_max_word``（细粒度，尽量多建词）；
   查询 ``ik_smart`` 之后接一道中文停用词（``QUERY_ANALYZER``）。只写
   ``analyzer`` 不写 ``search_analyzer`` 时查询也会走细粒度，问句被切碎、
   召回率反而下降；而查询侧不去掉疑问词，BM25 会把「只出现一次的常见词」
   当成罕见特征打分（见 ``QUERY_STOPWORDS`` 的注释）。
3. **「库 + 版本」是配对条件**。每对之间是 ``should`` + ``minimum_should_match=1``；
   写成两个独立的 ``terms``（即两个 ``IN``）会匹配出「A 库配 B 库的版本」这种
   不存在的组合。
4. **多词查询要命中不止一个词**（``MINIMUM_SHOULD_MATCH``）。停用词去掉的是
   「问句框架」，域内高频词（``设备`` / ``证书`` 这类）去不掉，只沾上一个的切片
   必须被这道闸拦在外面。
"""

from __future__ import annotations

from collections.abc import Sequence

from app.models.chunk import (
    CHUNK_TYPE_REFERENCE_POINTER,
    DEFAULT_DOC_VERSION,
    DEFAULT_INDEX_VERSION,
)

# 可检索文本的字段名。切片原文另存 ``content``（只存不索引），用来原样回给调用方。
RETRIEVAL_TEXT_FIELD = "retrieval_text"

#: 切片类型字段（#152）。**存进关键词文档是为了过滤它**：纯路标切片只用来建边，
#: 不参与召回——它进候选池只会占名额，还会把「命中一句『参见别处』」变成一次成功召回。
CHUNK_TYPE_FIELD = "chunk_type"

#: 稿号字段（#174）。值是「这一片属于第几稿」（``chunks.doc_version`` 的副本）。
DOC_VERSION_FIELD = "doc_version"

#: 「当前稿」标记（#174）。**召回只用一个布尔 term 过滤它**。
#:
#: 为什么是布尔而不是「按文档枚举稿号」：父 spec 明确否决了成对枚举
#: ``(document_id, doc_version)`` 子句——那种写法的查询体会随库里的文档数一起长，
#: 库一大就是一次必现的超时。布尔的作用域是「这一片」，索引期算好，召回时只剩一个
#: 常量条件，查询体大小与数据量无关。
CURRENT_DRAFT_FIELD = "is_current"

# 写入 / 查询分词器：写入细粒度、查询智能模式。
INDEX_ANALYZER = "ik_max_word"
SEARCH_ANALYZER = "ik_smart"

# 查询侧分析器 = ``ik_smart`` + 停用词。名字带 ``query`` 是为了让 mapping 一眼看出
# 「这个分析器只给查询用」，索引侧的 ``analyzer`` 仍是 ``INDEX_ANALYZER``。
QUERY_ANALYZER = "ik_smart_query"
QUERY_STOP_FILTER = "cn_query_stop"

# 查询侧停用词表。
#
# **为什么是显式词表而不是 ES 内置的 ``_chinese_``**：``stop`` 过滤器确实接受
# ``_chinese_`` 这个预置名，但在 ES 8.x 里它解析到 Lucene CJK 包里的
# ``stopwords.txt``，而那份文件里装的是**英文**停用词（``the`` / ``and`` / ``of`` …）。
# 实测（``_analyze``）：``"the thing"`` 只剩下 ``thing``，而
# ``"的 是 什么 怎么 哪些"`` 一个都没被去掉——预置名在这里等于什么都没做。
# 详情见 tests/test_keyword_query_builder.py 里那条用例的注释。
#
# **为什么这些词必须去掉**：票 8 的 v2 评测现场——问 ``E1025 是什么意思``，BM25 的
# 第一名是「值班交接」。链路是这样的：IK 把问句切成 ``[e1025, 是什么, 意思]``，
# 而 ``是什么`` 在 240 片语料里只出现 1 次（就在「值班交接」那节），罕见词 →
# IDF 5.08 → 一条与问题毫无关系的切片拿了 5.33 分，压过真正命中 ``E1025`` 的
# 那片（3.96）。**常见词被当成罕见特征**，这是 BM25 的经典翻车。
#
# 分两段：前一段是「问句框架」（疑问词 + 指代 + 时间副词），IK 会把
# 「是什么」「怎么办」「怎么回事」整体切出来，所以多字条目必须原样列；
# 后一段是单字虚词与语气词。内容词（``退款`` / ``证书`` / ``设备`` …）
# 一个都不许进这张表——那是召回本身。
QUERY_STOPWORDS: tuple[str, ...] = (
    # 问句框架
    "什么",
    "是什么",
    "什么意思",
    "意思",
    "什么的",
    "怎么",
    "怎么办",
    "怎么样",
    "怎么回事",
    "如何",
    "哪些",
    "哪个",
    "哪里",
    "为什么",
    "是多少",
    "多少",
    "几个",
    "这个",
    "那个",
    "之后",
    "以后",
    "之前",
    "请问",
    "一下",
    "一个",
    "一台",
    "要不",
    "是不是",
    "有没有",
    "能不能",
    "要不要",
    # ── 有争议的一条：``一般`` ──
    # 它既是填充词（「背调一般问哪些问题」），又是业务术语（安全事件分级里的
    # 「一般事件」、资质里的「一般纳税人」）。留在这里是因为两种用法的**代价不对称**：
    #   · 当填充词不删它 → 它会去匹配别的章节，把正确切片挤出前五（正是本次修复的病）；
    #   · 当术语删了它 → 它只是修饰语，真正区分的「事件」「纳税人」仍在查询里，
    #     召回不受影响（这一点靠 IDF 也帮不上：本语料 245 片里它出现 9 次，IDF≈3.2，
    #     不算罕见词，靠分数压不住它）。
    # 唯一的反例是「整句只有一个实词，且那个实词就是一般」。出现这种问法时，这条要重新评估。
    "一般",
    # 单字虚词与语气词
    "的",
    "了",
    "是",
    "在",
    "有",
    "和",
    "与",
    "及",
    "就",
    "都",
    "也",
    "还",
    "而",
    "但",
    "这",
    "那",
    "个",
    "我",
    "我们",
    "你",
    "他",
    "她",
    "它",
    "吗",
    "呢",
    "吧",
    "啊",
    "呀",
    "么",
    "之",
    "其",
    "被",
    "把",
    "让",
    "给",
    "对",
    "从",
    "用",
    "要",
    "想",
    "做",
    "会",
    "能",
)

# 多词查询的最小命中数（``multi_match`` 的 ``minimum_should_match``）。
#
# 取值是**实测**出来的，不是拍的。在同一份 240 片语料 + 24 道题上只跑 BM25 单路：
#
# | 取值 | P@5 | R@5 | MRR | R@1 | exact_hit@1 |
# | --- | --- | --- | --- | --- | --- |
# | 不加（基线） | .2524 | .8333 | .7222 | .5238 | 5/7 |
# | 70%（票面建议的起点） | .4524 | .6905 | .7024 | .5952 | 7/7 |
# | **50%（本值）** | .4135 | .7381 | .7500 | .6190 | 7/7 |
#
# 70% 把 4-6 个内容词的问句（``证书过期了要做哪些步骤``、``P1 故障的响应时限``…）
# 打成**空结果**：这些问句里有一半的词在目标切片里根本不出现，而 best_fields 的
# 百分比是**按每个字段内部的词数**向下取整的（6 个词 × 70% = 4 个必须命中）。
# R@5 因此从 .8333 掉到 .5952、MRR 也低于基线，属于「修一个洞、开三个洞」。
#
# 50% 是「4 个词以上必须命中 2 个」的最小值：3 个词以内向下取整到 1（``E1025``
# 这类单词查询照旧 100% 命中），4 个词以上要求 2 个——只沾上一个域内高频词的切片
# 进不来，而目标切片只要覆盖两个内容词就还在。
#
# 它和停用词表是**两道互补的闸**，不能互相替代：停用词治的是「罕见疑问词被当成
# 罕见特征」，这道百分比治的是「常见内容词单独撑起一条结果」。
MINIMUM_SHOULD_MATCH = "50%"

# 返回给调用方要用的字段（``_source`` 白名单），别把 embedding 之类的整条拖回来。
SOURCE_FIELDS = (
    "tenant_id",
    "kb_id",
    "index_version",
    "document_id",
    "chunk_id",
    "title",
    "content",
)


def build_mapping_update() -> dict:
    """存量物理索引要补的那一小块 mapping（#174，additive ``put_mapping`` 的载荷）。

     **为什么需要它**：物理索引是第 11 步建的，那时还没有 ``doc_version`` /
     ``is_current`` 两个字段。ES 对**未映射**字段的 ``term`` 查询不报错，只是命中 0 条
    ——症状是关键词路静默全空，而向量路照常返回结果，混合检索看起来只是「少了一路」。

     为什么补 mapping 而不是重建索引：``settings`` 与 ``analysis`` 是索引级的、不能后补，
     但**字段**可以 additive 地加。存量索引里的老文档没有这两个键，它们的 ``is_current``
     term 匹配不到——那正是「老数据没有稿号信息」的如实结果，由对账脚本而不是检索来管。
     **载荷只含 ``properties``**：带上 settings 会被 ES 直接拒绝。

     只有这两个字段在这里定义一次：:func:`build_es_mapping` 与补丁共用同一份，写两遍
     迟早一边加了字段、另一边漏掉。
    """

    return {
        "properties": {
            # 稿号：过滤维度，精确匹配不分词（以后「按稿只读直查」也要用它）。
            DOC_VERSION_FIELD: {"type": "keyword"},
            # 当前稿标记：只做布尔匹配，召回时是一个常量 term。
            CURRENT_DRAFT_FIELD: {"type": "boolean"},
        }
    }


def build_es_mapping() -> dict:
    """返回建索引的请求体（``indices.create(index=..., **build_es_mapping())``）。

    单分片零副本是**单机开发环境**的默认值：ES 的默认副本数是 1，单节点集群
    永远到不了 green，健康检查会一直黄。
    """

    return {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
            # 查询侧分析器要在建索引时一起注册：ES 的自定义分析器是**索引级设置**，
            # 不能在建好索引之后再补。改这张表必须删索引重建——线上靠「删掉
            # rag_chunks 再重新入库」，评测脚本靠「默认全量重建」；
            # 它的 --reuse 分支会先校验目标索引里有没有 QUERY_ANALYZER，旧索引直接拒跑，
            # 免得用旧口径跑新代码还看着一切正常。
            "analysis": {
                "filter": {
                    QUERY_STOP_FILTER: {
                        "type": "stop",
                        "stopwords": list(QUERY_STOPWORDS),
                    }
                },
                "analyzer": {
                    QUERY_ANALYZER: {
                        "type": "custom",
                        "tokenizer": SEARCH_ANALYZER,
                        "filter": [QUERY_STOP_FILTER],
                    }
                },
            },
        },
        "mappings": {
            "properties": {
                # 过滤维度：精确匹配，不参与分词。
                "tenant_id": {"type": "keyword"},
                "kb_id": {"type": "keyword"},
                "index_version": {"type": "keyword"},
                "document_id": {"type": "keyword"},
                "chunk_id": {"type": "keyword"},
                # 切片类型：只做精确匹配（过滤路标切片），不参加打分也不分词。
                CHUNK_TYPE_FIELD: {"type": "keyword"},
                # 可检索文本：写入细粒度（``analyzer``）、查询侧带停用词
                # （``search_analyzer``），两个都要显式声明。
                "title": {
                    "type": "text",
                    "analyzer": INDEX_ANALYZER,
                    "search_analyzer": QUERY_ANALYZER,
                },
                RETRIEVAL_TEXT_FIELD: {
                    "type": "text",
                    "analyzer": INDEX_ANALYZER,
                    "search_analyzer": QUERY_ANALYZER,
                },
                # 切片原文：只存不索引，响应里给调用方的必须是原文而不是检索文本。
                "content": {"type": "text", "index": False},
                # 稿号与「当前稿」标记：定义在 ``build_mapping_update`` 一处，
                # 存量索引的补丁用同一份（#174）。
                **build_mapping_update()["properties"],
            }
        },
    }


def build_bm25_query(
    *,
    tenant_id: str,
    kb_ids: Sequence[str],
    search_query: str,
    top_k: int,
) -> dict:
    """返回 ``search`` 的请求体：租户过滤 + 库范围 + multi_match 打分。

    ``kb_ids`` 是这次要查的库（现场九·第四步起不再带版本）：代下放到篇级之后，一次检索
    里每篇各有自己的当前代，库级版本不再是可见性判据。关键词索引里每篇**只有一份**
    （写入总是「先按文档全清、再写新的一份」），所以按库过滤就够了，不会重复。
    """

    scope = {"terms": {"kb_id": list(kb_ids)}}

    return {
        "size": top_k,
        "track_total_hits": False,
        "_source": list(SOURCE_FIELDS),
        "query": {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": search_query,
                            # title^2：标题命中比正文命中更值钱（教程里的原始取舍）。
                            "fields": ["title^2", RETRIEVAL_TEXT_FIELD],
                            "type": "best_fields",
                            # 只命中一个词不算命中：口径与理由见 MINIMUM_SHOULD_MATCH。
                            "minimum_should_match": MINIMUM_SHOULD_MATCH,
                        }
                    }
                ],
                "filter": [
                    {"term": {"tenant_id": tenant_id}},
                    # 库范围（现场九·第四步）：一次检索里每篇各有自己的当前代，库级版本
                    # 不再是可见性判据，范围只剩「哪几个库」。
                    scope,
                    # 只看当前稿（#174）：与向量侧 ``current_draft_filter()`` 同一个口径，
                    # 只是一个布尔 term。漏掉它，两稿切片并存时关键词路会多召回一份旧稿，
                    # 而结果看起来完全正常（同一段内容出现两次）。
                    {"term": {CURRENT_DRAFT_FIELD: True}},
                ],
                # 路标切片不进候选（#152）：它没有信息量，命中它等于没命中。
                # ``must_not`` 里没有这个字段的老文档不受影响（term 不匹配即通过）。
                "must_not": [{"term": {CHUNK_TYPE_FIELD: CHUNK_TYPE_REFERENCE_POINTER}}],
            }
        },
    }


def build_purge_query(
    *,
    document_id: str,
    tenant_id: str,
    index_version: str | None = None,
    doc_version: int | None = None,
) -> dict:
    """返回 ``delete_by_query`` 的请求体：只删这个租户的这一份文档。

    ``tenant_id`` 与 ``document_id`` 都是 ``term`` 且都在 ``filter`` 里：
    少一个就会删到别的租户同 id 的行（ADR-0006 的强制过滤同样适用于删除）。

    ``index_version`` 非空时再加一条版本过滤（#142）：重建新版本只清目标版本，
    线上那一版原样留着。物理索引本来就按版本隔离，这条过滤是第二道防线——它会在
    「写进了错误物理索引」时把误删挡住，而那种错一旦发生，删掉的就是线上数据。

    ``doc_version`` 非空时再加一条稿号过滤（#173）：重建第 2 稿只清第 2 稿的关键词文档，
    旧稿原样留着。这条过滤是**必须的**（不像版本过滤只是第二道防线）：同一张物理索引里
    本来就同时装着这篇文档的每一稿，少了它，重建一次就把旧稿从关键词那一路抹掉了，
    而向量那一路还在——两路对同一稿给出不同答案。``None`` = 这篇文档的所有稿。
    """

    filters: list[dict] = [
        {"term": {"tenant_id": tenant_id}},
        {"term": {"document_id": document_id}},
    ]
    if index_version is not None:
        filters.append({"term": {"index_version": index_version}})
    if doc_version is not None:
        filters.append({"term": {DOC_VERSION_FIELD: doc_version}})

    return {"query": {"bool": {"filter": filters}}}


def build_current_draft_update(*, document_id: str, tenant_id: str, doc_version: int) -> dict:
    """返回「把这篇文档的当前稿标记翻到某一稿」的 ``update_by_query`` 请求体（#175）。

    ## 为什么是 ``update_by_query`` 而不是「逐条改」

    这篇文档的切片数随文档长短变化，逐条 ``update`` 就是 N 次往返，而它跑在请求提交之后
    的那一小段时间里。``update_by_query`` 是一条请求打完整批。

    ## 脚本为什么读 ``_source`` 而不是 ``doc[...]``

    mapping 里 ``doc_version`` 是 **keyword**：``doc['doc_version'].value`` 取出来是字符串
    ``"3"``，与写进去的整数 3 比较永远不等——那会把**所有**切片标成「非当前稿」，症状是
    关键词路静默全空（向量路照常返回，看起来只是少了一路）。``ctx._source`` 是写入时的
    原样值（整数），所以比较成立。

    ## 为什么过滤条件两个都在 ``filter`` 里

    ``tenant_id`` 与 ``document_id`` 少一个就会翻到别的租户 / 别的文档的切片：前者是越权，
    后者是把别人的当前稿标记搅乱——两者都不报错。
    """

    return {
        "query": {
            "bool": {
                "filter": [
                    {"term": {"tenant_id": tenant_id}},
                    {"term": {"document_id": document_id}},
                ]
            }
        },
        "script": {
            "lang": "painless",
            "source": (
                f"ctx._source.{CURRENT_DRAFT_FIELD} = "
                f"(ctx._source.{DOC_VERSION_FIELD} == params.{DOC_VERSION_FIELD})"
            ),
            "params": {DOC_VERSION_FIELD: doc_version},
        },
    }


#: 对账聚合里「按文档分组」的那一层（#179）。名字只在请求体里出现一次，用常量是因为
#: 解析响应的一方要按同一个键去取——两处各写一个字符串，改一处就会静默读出空结果。
CURRENT_DRAFT_VERSIONS_AGG = "documents"

#: 每篇文档最多数几个不同的「当前稿」稿号。正常只有一个；多于一个就是分歧的现场
#: （对账要把它列出来）。取 10 而不是 1：截断会让「两个稿号都被标成当前稿」这种
#: 分歧看起来只有一个，而它恰恰是要抓的那一条。
CURRENT_DRAFT_BUCKETS_PER_DOCUMENT = 10


def build_current_draft_versions_query(*, tenant_id: str, document_ids: list[str]) -> dict:
    """返回「这批文档里**标成当前稿**的切片按文档分组数稿号」的搜索请求体（#179）。

    对账专用（``scripts/verify_document_versions.py``）：它问的是「关键词索引认为哪一稿
    是当前稿」，与库里那条指针比。**只取聚合，不取命中**（``size: 0``）：一篇文档的
    当前稿切片可能有几百片，把正文捞回来只为了读两个字段是白花带宽。

    ## 为什么三个过滤条件一个都不能省

    * ``tenant_id``：少了它就会把别的租户的稿号数进来（ADR-0006 的强制过滤同样适用于
      对账——它是一句按租户隔离的读，不是跨租户统计）；
    * ``is_current``：这就是被核对的那一个布尔（#174 的「当前稿」标记）；
    * ``document_id``：限量到本次扫描的那一批文档，否则聚合会把整个索引的文档都数一遍
      （库一大就是一次必现的超时）。

    ## 为什么按 ``document_id`` 分组、再在组内数 ``doc_version``

    ``doc_version`` 在 mapping 里是 keyword，两项 ``terms`` 聚合都成立；而**两层**聚合
    才能同时回答「这篇文档有没有标当前稿」与「标的是哪一稿」。只做一层（数 doc_version）
    就会丢掉「哪个稿号属于哪篇文档」，那正好是配对关系（ADR-0010 §5 同一条规矩）。
    """

    return {
        "size": 0,
        "query": {
            "bool": {
                "filter": [
                    {"term": {"tenant_id": tenant_id}},
                    {"term": {CURRENT_DRAFT_FIELD: True}},
                    {"terms": {"document_id": list(document_ids)}},
                ]
            }
        },
        "aggs": {
            CURRENT_DRAFT_VERSIONS_AGG: {
                "terms": {"field": "document_id", "size": max(len(document_ids), 1)},
                "aggs": {
                    "drafts": {
                        "terms": {
                            "field": DOC_VERSION_FIELD,
                            "size": CURRENT_DRAFT_BUCKETS_PER_DOCUMENT,
                        }
                    }
                },
            }
        },
    }


def build_keyword_documents(chunks: list[dict]) -> list[dict]:
    """把索引编排的切片字典转成 ES 文档（``_id`` 用 chunk_id，写入天然幂等）。

    关键口径：**可检索字段装检索文本**（``build_retrieval_text`` 的产物），
    与向量侧共用一份文本；切片原文另存 ``content``（mapping 里只存不索引），
    这样 ``mode=bm25`` 回给调用方的仍是原文，而不是「文档：… 正文：…」。

    **稿号与「当前稿」标记**（#174）从切片字典的**顶层**透传：``doc_version`` 是这一片
    属于第几稿，``is_current`` 是索引期算好的布尔（召回只过滤它，不枚举文档）。
    两个键都缺省成「第 1 稿 / 当前稿」——老调用点（评测脚本、离线重建）不传它们，
    而写空值会让 ``is_current`` 的 term 一条都匹配不到。
    """

    return [
        {
            "tenant_id": chunk["tenant_id"],
            "kb_id": chunk["kb_id"],
            # 索引版本是数据范围的一部分：缺省值与 chunks 列默认值同源。
            "index_version": chunk.get("index_version") or DEFAULT_INDEX_VERSION,
            "document_id": chunk["document_id"],
            "chunk_id": chunk["id"],
            "title": (chunk.get("meta") or {}).get("title", ""),
            RETRIEVAL_TEXT_FIELD: chunk["retrieval_text"],
            "content": chunk["content"],
            # 只为了过滤（路标切片不参与召回）；没有类型的老切片留空串。
            CHUNK_TYPE_FIELD: (chunk.get("meta") or {}).get("chunk_type", ""),
            # 稿号（#173 + #174）：**只写这一处**。它既是 #173 的删除范围里那条可匹配的
            # 字段（``build_purge_query`` 的稿号 term 查的就是它），也是 #174 的过滤维度
            # （mapping 里 ``DOC_VERSION_FIELD`` 是 keyword）。值形态由 ``_doc_version_of``
            # 统一钉成整数、缺省与 ``chunks.doc_version`` 列默认值同源——ES 的 keyword
            # 做 term 匹配，写成字符串 "2" 与整数 2 是两个不同的值，所以形态必须一致。
            DOC_VERSION_FIELD: _doc_version_of(chunk),
            # 「这一片是不是当前稿」：召回时的唯一过滤条件（#174）。
            CURRENT_DRAFT_FIELD: _is_current_of(chunk),
        }
        for chunk in chunks
    ]


def _doc_version_of(chunk: dict) -> int:
    """切片字典里的稿号；缺省与 ``chunks.doc_version`` 的列默认值同源。"""

    version = chunk.get("doc_version")
    return version if isinstance(version, int) else DEFAULT_DOC_VERSION


def _is_current_of(chunk: dict) -> bool:
    """切片字典里的「当前稿」标记；缺省按当前稿解释。

    为什么缺省是 ``True`` 而不是 ``False``：写 ``False`` 会让**所有**老调用点写进去的
    切片在召回时被这个 term 过滤掉——关键词路静默全空，比多召回一份旧稿难查得多。
    索引期一定会显式给值（``indexing_service`` 按文档指针算），缺省只服务离线脚本。
    """

    flag = chunk.get("is_current")
    return flag if isinstance(flag, bool) else True
