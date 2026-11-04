"""上传文档接口（同步索引闭环）。

步骤 5 填充：POST /api/v1/documents/upload
kb_id 放 body，不放路径参数。入参 tenant_id / kb_id / title / content。
出参 document_id / kb_id / status / chunk_count。
"""