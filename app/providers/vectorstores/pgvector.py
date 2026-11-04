"""pgvector 实现。

步骤 4 填充。相似度用 1 - (embedding <=> :vec) 转成「越大越相关」；
检索必须带 tenant_id 与 kb_id 过滤，否则会跨库召回。
"""