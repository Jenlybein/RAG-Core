# 0008 · 双入口错误契约：共享错误词汇，各自翻译

**Status**: Accepted

## 一句话决定

`ErrorCode` / `AppError` 放在 `app/core/`，与协议无关；「异常翻译成什么响应」是每个入口自己的适配器。HTTP 入口（本步实现）返回 `{code, msg, data}` 加 HTTP 状态码；第 11 步（目录 `11-MCP接入/`）的 MCP 入口按 JSON-RPC 用 `isError: true` + 文本带 `code` 表达工具执行失败。本步不写任何 MCP 代码。

## 为什么先讲这个

挂载的子应用不经过根 FastAPI 的依赖注入与异常处理器；工具里的异常在协议层就被翻译掉了，不会冒到 HTTP 那一层。如果现在把 `app/api/error_handlers.py` 写成「全局唯一出口」，第 11 步挂载 MCP 入口时必然返工。所以要先划清：哪些东西两个入口共用，哪些不共用。

## 决定：共用的与不共用的

**共用**——一份与协议无关的词汇表。业务层只写 `raise AppError(...)`，不认识 HTTP，也不认识 MCP。

```python
# 业务层（Service）里抛出的那一句
raise AppError(ErrorCode.KNOWLEDGE_BASE_NOT_FOUND)   # 20008
```

**这段在干什么**：给「知识库不存在」一个稳定编号。两个入口看到的是同一个 `20008`，不需要第二张码表；这一段可以直接粘进任何 Service 函数里。

**不共用**——响应壳。翻译由入口各自负责：

| 入口 | 适配器 | 失败怎么表达 |
| --- | --- | --- |
| HTTP API `/api/v1/*` | `app/api/error_handlers.py`（本步实现） | HTTP 200 + 非零 `code`，或 HTTP 500 + `50000`；响应体 `{code, msg, data}` |
| MCP 入口 `/mcp/` | 第 11 步实现（本步只留接缝） | HTTP 200 + JSON-RPC `result.isError = true`，文本带 `code`，例如 `ToolError("20008|knowledge base not found")` |

`app/main.py` 只做组装：调 `setup_logging()`、加请求上下文中间件、调 `install_handlers(app)`。

**你现在应该看到**：`AppError` 里没有 HTTP 状态码，也没有 JSON-RPC 字段。如果这里有，说明词汇表被入口污染了。

## 为什么不让两个入口用同一个响应壳

- JSON-RPC 的「工具执行失败」和「协议层错误」是两回事。业务失败是前者，用 `isError`；方法不存在才是 JSON-RPC `error`。硬套 HTTP 状态码会丢掉这层语义。
- 协议层不对称是真实存在的：HTTP 调用方先看状态码再看 `code`，MCP 调用方读 `isError` 并从文本取 `code`。两边判断方式不同，但编号相同。
- 已实测 `ToolError` 的文本会原样透传到 `isError` 结果里，普通异常会被包成通用文本从而丢掉 `code`——所以第 11 步要显式做 `AppError → ToolError` 翻译。

## 代价

- 每个入口都要写一份翻译层。新增入口时，有人会想「复用 HTTP 那套」，得靠这份 ADR 拦住。
- 调用方不能只学一种响应解析：HTTP 侧是两层判断，MCP 侧是 `isError` + 解析文本里的码。
- 错误码必须一次写全、只追加不改编号。两份翻译层都引用它，改编号会同时破坏两边。
- 第 11 步（目录 `11-MCP接入/`）的三篇教程有三处已知缺口（缺 `AppError → ToolError` 翻译、验收表与裸 dict 冲突、`request_id` 承诺落空）；本 ADR 只记录，不修那些文件。

## 接着验证

读 `app/core/exceptions.py` 与 `app/api/error_handlers.py`：前者应找不到任何 HTTP / JSON-RPC 知识，后者应明确是 HTTP 入口适配器。第 2 步三篇教程里不应再把它写成「全系统唯一出口」。

**自检**：一个没读过本项目的人，能否只看这份 ADR 说清「为什么 MCP 入口不返回 `{code,msg,data}`」？说不清就补大白话，而不是补术语。
