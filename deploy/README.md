# deploy/ — 本地基础设施

两套栈、一个 compose 文件，按 **profile** 分开起：

| 栈 | 起法 | 里面有什么 |
| --- | --- | --- |
| 业务栈（默认） | `docker compose -f deploy/docker-compose.yml up -d` | postgres（pgvector）、redis、worker、（按阶段再开）elasticsearch |
| 观测栈（`obs`） | `docker compose -f deploy/docker-compose.yml --profile obs up -d` | langfuse-web、langfuse-worker、clickhouse、minio、langfuse-redis |

业务最小部署**不需要**观测栈：不带 `--profile obs` 时那五个容器根本不会被创建
（`docker compose ... config --services` 里看不到它们）。观测栈只写文件也能过活，
它挂了检索照常返回——这是 ADR-0021 的「观测是旁路」，不是一句口号。

```bash
# 业务栈
docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml up -d postgres   # 只起一个
docker compose -f deploy/docker-compose.yml down             # 停（保留数据卷）
docker compose -f deploy/docker-compose.yml down -v          # 连数据卷一起删（下次 init SQL 会重跑）

# 观测栈（一整套）
docker compose -f deploy/docker-compose.yml --profile obs up -d
docker compose -f deploy/docker-compose.yml --profile obs up -d langfuse-web   # 只重启一个
docker compose -f deploy/docker-compose.yml --profile obs down                 # 停（保留数据卷）
```

注意 `--profile obs` 会把**无 profile 的业务服务一起带上**（Postgres / Redis / ES / worker 的
compose 定义）。这是刻意的：观测栈要用的 Postgres 就是业务那一个实例，只是用另一个**库**
（见下面「观测栈」）。已经在跑的业务服务不受影响，`up -d` 只会补齐缺的。

## 服务一览

业务栈：

| 服务 | 容器名 | 端口 | 从哪一步需要 |
| --- | --- | --- | --- |
| postgres（`pgvector/pgvector:pg16`） | `pg16-pgvector` | 5432 | 阶段 1 |
| redis（`redis:7-alpine`） | `rag-center-redis` | 6379 | 阶段 3 |
| worker（`deploy/Dockerfile` 自建） | `rag-worker` | — | 阶段 3 |
| elasticsearch（官方 8.17.10 + 自建 IK） | `rag-es` | 9200 | 阶段 4 |

观测栈（`profiles: ["obs"]`）：

| 服务 | 容器名 | 端口（宿主） | 作用 |
| --- | --- | --- | --- |
| langfuse-web（`langfuse/langfuse:$LANGFUSE_IMAGE_TAG`） | `rag-langfuse-web` | **3000** | UI + Public API + tRPC |
| langfuse-worker（`langfuse/langfuse-worker:$LANGFUSE_IMAGE_TAG`） | `rag-langfuse-worker` | 仅栈内 3030 | 消费写入队列、跑 ClickHouse 迁移 |
| clickhouse（`clickhouse/clickhouse-server:26.3.62.3`） | `rag-langfuse-clickhouse` | `127.0.0.1:8123` | 事件与 observation 存储 |
| minio（Chainguard，按 digest 钉住） | `rag-langfuse-minio` | **9090**（S3 API）、`127.0.0.1:9091`（控制台） | 事件对象存储 |
| langfuse-redis（`redis:7-alpine`） | `rag-langfuse-redis` | — | worker 队列（不映射宿主端口） |

宿主侧真正需要打开的就两个：**3000**（UI / API）与 **9090**（MinIO S3，预签名 URL 用的就是它）。
ClickHouse 的 8123 与 MinIO 控制台的 9091 只绑在 `127.0.0.1`，方便用
`clickhouse-client` 查一眼，不对外。观测栈的 Redis 不映射端口：业务 Redis 已经占着 6379，
而这个 Redis 只服务栈内的 worker。

## 观测栈：资源占用、版本、首次使用

### 资源占用（实测，2026-11-11，Windows + Docker Desktop）

`docker stats` 在**空栈 + 已灌一条 trace** 时读到：

| 容器 | 内存 |
| --- | --- |
| `rag-langfuse-web` | ≈ 750 MB |
| `rag-langfuse-worker` | ≈ 660 MB |
| `rag-langfuse-clickhouse` | ≈ 375 MB |
| `rag-langfuse-minio` | ≈ 73 MB |
| `rag-langfuse-redis` | ≈ 8 MB |
| **合计** | **≈ 1.8 GB** |

所以按 **1.5~2 GB** 估：够跑，但和 PG + ES（ES 单机就 ~1.3 GB）同机时，16 GB 的机器勉强，
8 GB 的机器会开始换页。三个提示：

- 磁盘涨得最快的是 ClickHouse（`langfuse_clickhouse_data` 卷），保留期与采样由栈自己的配置管。
- 首次启动比之后慢：web 要跑 474 个 Prisma 迁移，worker 要建 ClickHouse 表。
- 只是查「哪一步慢」而不需要 UI 的机器，**不要起这个栈**，用 `TRACE_SINK=jsonl`（见「降级」）。

### 版本与兼容矩阵

| 组件 | 本仓钉的 | 约束 |
| --- | --- | --- |
| 服务端 | `LANGFUSE_IMAGE_TAG` 默认 `4`（实测拉到 4.78.0） | **必须 ≥ 4.59.0** |
| Python SDK | `langfuse==5.2.0`（`uv.lock` 钉死） | v5 只用 v4 的 Public API |
| ClickHouse | `26.3.62.3` | 上游 compose 的值，迁移有版本要求，别随手升 |
| MinIO | Chainguard 镜像，按 digest 钉住 | 上游 v4 compose 用的就是它 |

官方兼容矩阵：Python SDK **v5** 要求自托管服务端 **≥ v4.59.0**（SDK v3/v4 只需 ≥3.63.0，
但 Observations / Metrics API v2 要 v4）——见
[langfuse-docs 的版本兼容表](https://langfuse.com/self-hosting/upgrade/versioning#sdk-server)。
所以「服务端 v4 + SDK v5」是官方支持的一对，不是撞上的。

web 与 worker **必须同版本**（同一个 `LANGFUSE_IMAGE_TAG` 变量统一注入）：worker 落后一次
发布，写入形状就会错。想钉死小版本就改这个变量，别去改 compose 里那两行 `image:`。

### 数据落哪：独立库，不混业务库

观测数据进 Postgres 里**独立的 `langfuse` 库**，业务库 `rag_db` 里一张观测表都不多出来
（ADR-0021 决策 2：业务库只留结构，原文只进观测栈）。库由
[`deploy/init/02-langfuse-db.sql`](init/02-langfuse-db.sql) 在数据卷全新时建好；
数据卷已存在时手工补一次：

```bash
docker exec -it pg16-pgvector psql -U postgres -c "CREATE DATABASE langfuse;"
```

ClickHouse / MinIO / Redis 各自独占一个数据卷（`langfuse_*`），删了业务栈的卷不会带走
观测数据，反之亦然。

### 密钥：全是占位值

compose 里每一个 `local-dev-only-*` 都是**占位值**：本地调试够用，**别带到线上**。
仓库里不提交任何真密钥。要换真值时：

```bash
openssl rand -hex 32      # NEXTAUTH_SECRET / SALT / ENCRYPTION_KEY
openssl rand -base64 24   # CLICKHOUSE_PASSWORD / MINIO_ROOT_PASSWORD / REDIS_AUTH
```

改了 `minio` / `clickhouse` / `langfuse-redis` 的密码，**同一个值要改三处**：对应容器的
自己的环境变量、`x-langfuse-env` 里 web/worker 读的那一项、以及
`LANGFUSE_S3_*_SECRET_ACCESS_KEY`。少改一处表现为「栈起来了但事件写不进去」。

一个已经踩过的坑：`ENCRYPTION_KEY` 在 compose 里**必须加引号**。64 个 `0` 不加引号会被
YAML 读成数字 `0`，v4 于是启动即报
`ENCRYPTION_KEY must be 256 bits, 64 string characters in hex format`。

### 首次使用：进 UI 注册并拿 key

栈起来之后**必须**做一次，Langfuse 不会替你建项目：

1. 打开 <http://localhost:3000>，注册第一个账号（本地栈，随便填）。
2. 建一个 organization / project（名字随意，例如 `rag-core`）。
3. Project → **Settings → API Keys → Create**，抄下 public key 与 secret key。
4. 填进 `.env`（没有就从 `.env.example` 抄一份）：

```dotenv
TRACE_SINK=langfuse
LANGFUSE_HOST=http://localhost:3000
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

5. 重启 API 进程（`TRACE_SINK` 是启动时读的配置），然后检索一次。

不填 key 或没起栈时，`langfuse` 去向会在第一次用时失败——**只留一条
`observability trace not recorded` 的 warning**，检索照常返回（ADR-0021 决策 3）。

### 看一眼 trace 有没有对上

检索响应里有 `metadata.trace_id`（32 位十六进制，永远非空）。拿它去 UI 的 **Traces**
里搜，或直接问 API：

```bash
ID=6ef2c730a53e55f7b54ec252ad7665c8
curl --noproxy '*' -s -u "pk-lf-...:sk-lf-..." \
  "http://127.0.0.1:3000/api/public/v2/observations?traceId=$ID"
```

期望：`traceId` 与响应里的 `trace_id` **逐字符相同**，且能看到六段（无该段时不含该段）：

```text
query_optimization / vector_recall / keyword_recall / fusion / rerank / collapse
```

每段的真实耗时在 span 的 `metadata.duration_ms` 里（不是 span 自己量到的 latency：
六段在链路里已经量完了，这是「事后补记」，见 `app/observability/langfuse_sink.py`）。

本仓的一次实测（2026-11-11，本机，`langfuse 4.78.0` + Python SDK `5.2.0`）：

```text
log_id   = verify-104-1794351342
trace_id = 6ef2c730a53e55f7b54ec252ad7665c8     ← 与上面 /v2/observations 查到的 traceId 一致
六段      query_optimization 3.478ms / vector_recall 81.656ms / keyword_recall 12.525ms
          fusion 1.001ms / rerank 240.199ms / collapse 1.46ms
```

那次验证走的是**观测旁路本身**（`emit_retrieval_trace` + 真 `LangfuseTraceSink` + 真栈），
不是 `POST /api/v1/rag/retrieve`：本机 worktree 没有 `.env`，嵌入模型与 LLM 的 key 都不在，
检索链路起不来。`trace_id` 由与响应同一个函数（`trace_id_for`）派生，注入后从栈里读回。

## 起不来怎么办：降级到本地文件

观测栈**整套**起不来（拉不动镜像、内存不够、ClickHouse 崩了）时，不要为它折腾检索链路——
把去向切回文件即可，**六个容器一个都不用起**：

```dotenv
TRACE_SINK=jsonl                 # 默认值，就是这条
TRACE_JSONL_DIR=logs/traces      # 一次检索一行，按天轮转
TRACE_JSONL_RETENTION_DAYS=7     # 写入时清理超期文件，读取也只扫这个窗口
```

- 单行 ≤ 2 KB、按天轮转、只保留最近若干天；没有查询与聚合，「上周哪类查询变慢」只能 grep。
  这是**降级去向，不是替代品**（ADR-0021「代价」）。
- 只想知道「度量有没有跑」的机器可以用 `TRACE_SINK=none`：六段照样计时，只是没人收下。
  单测与 CI 就是这个值（`tests/conftest.py` 强制覆盖，且不发起任何真实外部调用）。
- 已经在跑 `langfuse` 去向、栈中途挂了：检索不会失败，每次都是一条 warning；恢复后
  新的检索自动又有 trace（`up -d` 重起即可，数据卷保留着历史）。

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
- `02-langfuse-db.sql`：`CREATE DATABASE langfuse;`（观测栈用独立库，别和业务表混）

数据卷已存在时，这两步要手动补：

```bash
docker exec -it pg16-pgvector psql -U postgres -d rag_db -c "CREATE EXTENSION IF NOT EXISTS vector;"
docker exec -it pg16-pgvector psql -U postgres -c "CREATE DATABASE langfuse;"
```

容器里的密码是 `123456`，`.env` 的 `DATABASE_URL` 必须与它一致（教程踩过
`.env.example` 写 `postgres:postgres` 导致 `password authentication failed` 的坑）。
观测栈的 web / worker 也吃同一个实例，只是把库名换成 `langfuse`。

## 本机配了 HTTP 代理时

`curl` 测本地端口会被代理接管并返回假的 502，测本地服务一律加 `--noproxy '*'`。
