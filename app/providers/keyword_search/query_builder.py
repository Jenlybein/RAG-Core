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

from app.models.chunk import DEFAULT_INDEX_VERSION

# 可检索文本的字段名。切片原文另存 ``content``（只存不索引），用来原样回给调用方。
RETRIEVAL_TEXT_FIELD = "retrieval_text"

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
            }
        },
    }


def build_bm25_query(
    *,
    tenant_id: str,
    index_versions: dict[str, str],
    search_query: str,
    top_k: int,
) -> dict:
    """返回 ``search`` 的请求体：租户过滤 + 库/版本配对 + multi_match 打分。

    ``index_versions`` 是「kb_id → 索引版本」的映射（与向量侧的范围版签名同形状），
    不是两个平行数组：配对关系只能从映射里读出来，数组一旦错位就会静默越权。
    """

    # 每对「库 + 版本」是一个独立的必须同时成立的子句；子句之间才是 should。
    pairing = [
        {
            "bool": {
                "filter": [
                    {"term": {"kb_id": kb_id}},
                    {"term": {"index_version": version}},
                ]
            }
        }
        for kb_id, version in index_versions.items()
    ]

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
                    # 配对条件必须留在 filter 里：若把 should 放在顶层 bool，同时又
                    # 有 must，ES 的默认 minimum_should_match 会退化成 0，
                    # 于是「库 + 版本」这对条件变成了纯加分项——过滤形同虚设。
                    {"bool": {"should": pairing, "minimum_should_match": 1}},
                ],
            }
        },
    }


def build_purge_query(*, document_id: str, tenant_id: str) -> dict:
    """返回 ``delete_by_query`` 的请求体：只删这个租户的这一份文档。

    ``tenant_id`` 与 ``document_id`` 都是 ``term`` 且都在 ``filter`` 里：
    少一个就会删到别的租户同 id 的行（ADR-0006 的强制过滤同样适用于删除）。
    """

    return {
        "query": {
            "bool": {
                "filter": [
                    {"term": {"tenant_id": tenant_id}},
                    {"term": {"document_id": document_id}},
                ]
            }
        }
    }


def build_keyword_documents(chunks: list[dict]) -> list[dict]:
    """把索引编排的切片字典转成 ES 文档（``_id`` 用 chunk_id，写入天然幂等）。

    关键口径：**可检索字段装检索文本**（``build_retrieval_text`` 的产物），
    与向量侧共用一份文本；切片原文另存 ``content``（mapping 里只存不索引），
    这样 ``mode=bm25`` 回给调用方的仍是原文，而不是「文档：… 正文：…」。
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
        }
        for chunk in chunks
    ]
