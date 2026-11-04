"""EmbeddingProvider 抽象。

步骤 4 填充。embed_texts（批量、要分批与重试）与 embed_query（单条、要低延迟）
是两个方法而不是一个方法加参数——两侧的扩展方向不同。
"""