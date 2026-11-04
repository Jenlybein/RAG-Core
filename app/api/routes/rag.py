"""检索接口。

步骤 5 填充：POST /api/v1/rag/retrieve
入参 tenant_id / kb_id / user_id / query，出参 retrieved_chunks[] + metadata。
边界：不返回 context_text，不调大模型，不生成答案——拼上下文是租户的事。
"""