"""OpenAI 兼容的 Embedding 实现（百炼走这个协议）。

步骤 4 填充。请求必须带 dimensions=1536；单批上限 20 条，取 10 留重试余量。
"""