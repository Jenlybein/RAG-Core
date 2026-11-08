# 对外接口参考

## 文档管理接口

### 上传文档

#### 请求

`POST /api/v1/documents/upload`，`multipart/form-data`。

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `kb_id` | 是 | 目标知识库，必须已存在且属于当前租户 |
| `file` | 是 | 单个文件，上限 100 MB |
| `title` | 否 | 缺省取文件名去掉扩展名 |
| `chunk_size` | 否 | 覆盖默认 800，允许 200–2000 |
| `chunk_overlap` | 否 | 覆盖默认 100，必须小于 `chunk_size` |

`chunk_size` 与 `chunk_overlap` 是**请求级覆盖**，只影响本次上传，不改变知识库配置。下一次上传不传就是默认值。

#### 响应

```json
{"code": 0, "msg": "success", "data": {"document_id": "doc_7f3a", "status": 3}}
```

`status=3` 表示处理中。上传接口**同步返回**，解析、切块、向量化都在异步任务里跑。客户端不要拿 3 当失败重试，重复上传会产生两份文档。

#### 错误码

| 码 | 含义 | 处置 |
| --- | --- | --- |
| `40001` | 知识库不存在 | 先查 `kb_id` 归属 |
| `40002` | 文件超过 100 MB | 拆分或压缩后重传 |
| `40003` | `chunk_overlap >= chunk_size` | 修正参数 |
| `50001` | 存储写入失败 | 检查磁盘与权限 |

### 查询文档状态

#### 请求

`GET /api/v1/documents/{document_id}`。返回 `status`、`chunk_count` 与 `failure_reason`。`status` 取值：1 待处理、2 排队中、3 处理中、4 已完成、5 失败。

#### 响应

`chunk_count` 只在 `status=4` 时可信，其余状态恒为 0——不要把 0 当成「切块结果为空」。判断是否真的空，要看 `status=4` 且 `chunk_count=0`。

## 检索接口

### 检索

#### 请求体

`POST /api/v1/rag/retrieve`，`application/json`。

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `query` | 是 | 用户问题，上限 2000 字 |
| `kb_ids` | 是 | 数组，一次可查多个库 |
| `top_k` | 否 | 默认 5，上限 50 |
| `rerank` | 否 | 布尔，默认 false |
| `index_versions` | 否 | `kb_id → 版本`的配对映射 |

`index_versions` 必须是**配对映射**，不能只传一行版本号。只传版本号时无法判断它对应哪个库，会编译成恒假条件，结果是「什么都没搜到」而不是报错。

#### 响应结构

```json
{"code": 0, "data": {"outcome": "ok", "results": [{"chunk_id": "...", "score": 0.83}]}}
```

`outcome` 只有三种取值：`ok`、`degraded`、`rejected`。`degraded` 时 `degraded_reason` 一定非空，且只允许四个固定英文串之一，不拼异常文本。

#### 降级字段

| 字段 | 何时出现 | 含义 |
| --- | --- | --- |
| `degraded_reason` | `outcome=degraded` | 固定枚举，可为 `vector_unavailable` / `keyword_unavailable` / `embedding_timeout` / `rerank_timeout` |
| `scores_vector` | 向量通道有结果 | 向量相似度，与 `score` 不同 |
| `scores_keyword` | 关键词通道有结果 | BM25 分数 |

`score` 在融合后是 RRF 分，不再是余弦相似度。旧调用方按余弦阈值过滤的逻辑必须改读 `scores_vector`，否则阈值全部失效。

## 知识库接口
### 创建知识库

#### 请求

`POST /api/v1/knowledge_bases`，字段 `name`（必填）、`description`（可选）、`chunk_size`（可选，默认 800）。知识库创建后 `chunk_size` 可以改，但只对之后上传的文档生效，存量文档不会自动重切。

#### 响应

返回 `kb_id`。`kb_id` 一旦生成不可修改，重命名只改 `name`。

### 列出知识库

#### 请求

`GET /api/v1/knowledge_bases`，支持 `page` 与 `page_size`，默认每页 20 条，上限 100。

#### 响应

返回当前租户下的全部知识库，按创建时间倒序。文档数量是**实时统计**，知识库很多时这个接口会变慢，前端不要高频轮询。

## 健康检查接口

### 健康检查

#### 请求

`GET /api/v1/health`，无需鉴权。

#### 响应

返回 `{"code": 0, "data": {"status": "ok"}}`。

这个接口只检查进程是否存活，**不检查数据库、Elasticsearch、Redis 是否可用**。要做依赖检查用 `/api/v1/health/deps`，它会逐项探活并按严重度排序返回。

## 版本查询接口

### 版本

#### 请求

`GET /api/v1/version`，无需鉴权。返回 `build`（构建号）、`commit`（提交哈希前 7 位）与 `started_at`（进程启动时间）。

`commit` 用来判断线上跑的是哪一个提交。报障时提供 `build` 与 `commit`，比提供部署时间更准确——同一个部署包可能被重启多次，`started_at` 会变而 `commit` 不变。

## 通用约定

### 错误返回格式

HTTP 状态码统一 200，业务错误放在 `code` 字段里。`code=0` 表示成功，其余都是业务错误。调用方必须判 `code`，只判 HTTP 状态码会把所有错误当成成功。

### 分页参数

`page` 从 1 开始，`page_size` 上限 100。超出上限不报错，按 100 截断——静默截断比报错更容易掩盖问题，调用方要自己校验返回条数。

### 请求追踪

每个响应都带 `X-Request-Id`。报障时必须提供这个值，否则无法定位到具体调用记录。
