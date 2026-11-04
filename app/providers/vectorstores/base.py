"""VectorStore 抽象。

步骤 4 填充：add_chunks / similarity_search / delete_by_document_id。
业务层只依赖这个抽象，换 Milvus / Qdrant 只改实现与工厂函数。
"""