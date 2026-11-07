# deploy/ — 本地基础设施

五个服务一次写全，按阶段逐步打开：postgres（阶段 1）→ redis + worker（阶段 3）→ elasticsearch（阶段 4）→ langfuse（阶段 7）。

```bash
docker compose -f deploy/docker-compose.yml up -d          # 全部
docker compose -f deploy/docker-compose.yml up -d postgres # 只起一个
docker compose -f deploy/docker-compose.yml down           # 停（保留数据卷）
docker compose -f deploy/docker-compose.yml down -v        # 连数据卷一起删（下次 init SQL 会重跑）
```

| 服务 | 容器名 | 端口 | 从哪一步需要 |
| --- | --- | --- | --- |
| postgres（`pgvector/pgvector:pg16`） | `pg16-pgvector` | 5432 | 阶段 1 |
| redis（`redis:7-alpine`） | `rag-center-redis` | 6379 | 阶段 3 |
| worker（`deploy/Dockerfile` 自建） | `rag-worker` | — | 阶段 3 |
| elasticsearch（官方 8.17.10 + 自建 IK） | `rag-es` | 9200 | 阶段 4 |
| langfuse（`langfuse/langfuse:2`） | `rag-langfuse` | 3000 | 阶段 7 |

## worker：异步索引执行者 + OCR 运行时

`worker` 与 API 共享代码镜像但跑不同命令，**必须用容器**（Celery 官方不支持 Windows，
且 OCR 原生运行时只备了 Linux x64 资产）。

```bash
docker compose -f deploy/docker-compose.yml build worker     # 首次或代码变更后
docker compose -f deploy/docker-compose.yml up -d worker
docker compose -f deploy/docker-compose.yml logs -f worker   # 看到 celery@<id> ready 即就绪
```

镜像内预置、运行期不联网：

| 资产 | 路径 | 版本 |
| --- | --- | --- |
| PDFium | `/opt/ocr/lib/libpdfium.so` | `native-v7988`（153.0.7988.0） |
| ONNX Runtime | `/opt/ocr/lib/libonnxruntime.so` | `1.27.0` |
| PP-OCRv6 Small 模型 | `/opt/ocr/models/pp-ocrv6-small/oar-ocr-v0.7.0` | `oar-ocr-v0.7.0`（约 31 MB） |

三个环境变量由镜像固化：`PDFIUM_LIB_PATH` / `ORT_DYLIB_PATH` / `PDF_INSPECTOR_MODEL_CACHE`，
配合 `OCR_OFFLINE=true` 保证 Worker 不去下载任何东西。

宿主侧若要手工拉同一套资产（用于 `pytest -m ocr`），跑 `bash deploy/ocr/fetch_runtime.sh`
——脚本幂等，带大小与 SHA-256 校验，第二次起全部缓存命中。

## 与教程的一处差异：ES 镜像

教程原先把 `elasticsearch` 指向第三方预装 IK 镜像
`registry.cn-hangzhou.aliyuncs.com/tulingfox/elasticsearch:8.17.10`（教程「装法 A」）。
**该镜像现在无法匿名拉取**：`docker pull` 走完 ACR 的匿名 token 流程后仍返回
`not found`（`docker manifest inspect` 同样失败）。

因此本仓库改为教程「装法 B」的等价做法，但把「装插件」从一次性操作变成构建步骤：

- `deploy/elasticsearch/Dockerfile`：`FROM docker.elastic.co/elasticsearch/elasticsearch:8.17.10`
  + `RUN bin/elasticsearch-plugin install --batch <IK 8.17.10 zip>`
- compose 里用 `build:` 构建，镜像名 `rag-center-elasticsearch-ik:8.17.10`

两条硬约束不变：

1. **IK 版本必须与 ES 完全同版**（改了 ES 版本就要同步改 Dockerfile 里的插件 URL），
   否则插件根本装不上，报 `plugin [analysis-ik] was designed for ... but is installed on ...`。
2. 装完必须验证分词真的生效，而不是「插件装了但 mapping 没写 `ik_max_word`」：

```bash
curl --noproxy '*' -X POST "http://127.0.0.1:9200/_analyze" \
  -H "Content-Type: application/json" \
  -d '{"analyzer":"ik_max_word","text":"退款申请流程"}'
# 期望切出 退款 / 申请 / 流程；返回整句一个 token 说明没生效
```

首次 `up -d` 会构建该镜像（下载官方 ES 约 2 GB + 插件包），比之后每次启动慢得多。

## postgres 的 init 脚本

`deploy/init/*.sql` 挂到 `/docker-entrypoint-initdb.d`，**只在数据卷全新时执行一次**：

- `01-pgvector.sql`：`CREATE EXTENSION IF NOT EXISTS vector;`
- `02-langfuse-db.sql`：`CREATE DATABASE langfuse;`（Langfuse 用独立库，别和业务表混）

数据卷已存在时，扩展要手动补：

```bash
docker exec -it pg16-pgvector psql -U postgres -d rag_db -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

容器里的密码是 `123456`，`.env` 的 `DATABASE_URL` 必须与它一致（教程踩过
`.env.example` 写 `postgres:postgres` 导致 `password authentication failed` 的坑）。

## 本机配了 HTTP 代理时

`curl` 测本地端口会被代理接管并返回假的 502，测本地服务一律加 `--noproxy '*'`。
