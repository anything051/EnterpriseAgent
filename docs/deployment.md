# V1.0 本地容器部署与恢复

`compose.release.yaml` 启动两个独立的应用。每个应用各有 PostgreSQL 数据卷和 Docker 网络。只有 UI 和 API 端口发布到宿主机的 `127.0.0.1`。库存服务通过企业 API 的网络命名空间提供回环 HTTP，企业 SQL 查询继续使用独立的只读角色。此配置用于本地演示和验收。

| 应用 | 界面 | API |
| --- | --- | --- |
| CourseRAG | [课程工作台](http://127.0.0.1:8501) | [课程 API](http://127.0.0.1:8001/docs) |
| EnterpriseAgent | [企业工作台](http://127.0.0.1:8502) | [企业 API](http://127.0.0.1:8002/docs) |

## 准备并启动

1. 在仓库根目录复制 `.env.release.example`，文件名为 `.env.release`。用 `openssl rand -hex 24` 分别生成八个不同值，填入各自字段。不同环境使用独立配置文件、Compose 项目名和端口。配置文件保留在本地。
2. 安装 Docker Compose 与项目的 Python 3.11 环境。本地 Ollama 需要支持工具调用的 Chat 模型和独立的 Embedding 模型。示例使用 `qwen3.5:9b` 与 `nomic-embed-text:latest`。在主机用 `curl -fsS http://127.0.0.1:11434/api/tags` 确认本地模型修订。
3. Docker Desktop 默认用 `host.docker.internal` 访问主机的 Ollama。原生 Linux Docker 需要显式设置 `RAG_CONTAINER_OLLAMA_BASE_URL` 为容器可达且获准访问的本地端点。Chat 与 Embedding 可分别选择供应商。设置为 OpenAI 时默认用官方 HTTPS 地址，只有显式指定 `RAG_CONTAINER_CHAT_BASE_URL` 或 `RAG_CONTAINER_EMBEDDING_BASE_URL` 才覆盖对应地址。

~~~bash
cp -n .env.release.example .env.release
chmod 600 .env.release
# 填写 .env.release 后继续
python3 tools/release/check_config.py --env-file .env.release
docker compose --env-file .env.release -f compose.release.yaml -p enterprise-rag-v1 config --quiet
docker compose --env-file .env.release -f compose.release.yaml -p enterprise-rag-v1 build course-api
docker compose --env-file .env.release -f compose.release.yaml -p enterprise-rag-v1 up -d --wait --no-build
~~~

Python 和 PostgreSQL 基础镜像使用摘要固定。两个界面及库存服务复用同一构建映像。发行容器使用非 root 账号、只读根文件系统、有限临时目录和日志轮转。独立运行某个应用时，在 `up` 命令末尾只指定 `course-ui` 或 `enterprise-ui`，Compose 会启动该应用的依赖。

按界面提示输入课程令牌或企业角色令牌。CourseRAG 可以分别导入 [操作系统 PDF](../data/samples/course_memory.pdf) 与 [链路层 Markdown](../data/course_rag/demo/network.md)。企业端用分析员令牌载入演示制度，执行华东销售、A100 库存和盘点时限的业务概览。[课程演示](course_rag_demo.md) · [企业演示](enterprise_agent_demo.md)

## 验证与日志

~~~bash
docker compose --env-file .env.release -f compose.release.yaml -p enterprise-rag-v1 ps
docker compose --env-file .env.release -f compose.release.yaml -p enterprise-rag-v1 logs --since 10m course-api enterprise-api
(
  set -a
  . ./.env.release
  set +a
  uv run --locked python tools/release/smoke.py --output /tmp/enterprise-v1-smoke.json
)
~~~

`/healthz` 检查进程存活，`/readyz` 检查应用账号能读取所需的 PostgreSQL 表。模型连通、只读销售角色和真实工具调用由验收脚本覆盖。日志使用服务端请求 ID，记录路由模板、状态和耗时。API 不缓存响应，错误不返回异常内容。Uvicorn 访问日志和应用追踪上传在发行配置中关闭。浏览器令牌按界面会话输入，UI 容器不接收应用令牌。

部署文件适用于本机回环场景。公开接入前需要独立验证 TLS、反向代理、账号与密钥管理、并发限流、备份保留和监控。

## 备份与还原演练

逻辑备份保留源文、二进制 PDF、索引、目录、会话、工具记录、表角色和对象 ACL。源、目标均应是已经按 V1.0 拆分的双数据库项目。若已有 V0.6 共库项目，原 `compose.yaml` 仍可运行。共库数据需按实际授权范围迁移后再导入独立库。

在停止写入、等待正在运行的任务结束后，分别备份两库。备份目录由命令新建，仅当前用户可访问，包含两个 dump 和 SHA-256 清单。将快照放在仓库外。模型端点、Embedding 修订、前缀、维度等索引配置应独立保存。恢复原企业会话还需要原角色令牌，因为会话所有权与令牌及角色绑定。

~~~bash
python3 tools/release/snapshot.py backup --env-file .env.release \
  --project enterprise-rag-v1 --archive /tmp/enterprise-v1-backup
~~~

另建 `.env.release.restore`，保留与源一致的模型和索引配置及会话令牌，设置新的数据库密码。为恢复项目分配新端口，例如 `18021`、`18022`、`18521`、`18522`。设置 `COURSE_RELEASE_DB_NAME=enterprise_rag_recovered` 与 `ENTERPRISE_RELEASE_DB_NAME=enterprise_rag_recovered`。在未导入其他发布环境变量的终端执行。

~~~bash
chmod 600 .env.release.restore
docker compose --env-file .env.release.restore -f compose.release.yaml -p enterprise-rag-recovery up -d --wait course-db enterprise-db
python3 tools/release/snapshot.py restore --env-file .env.release.restore \
  --project enterprise-rag-recovery --archive /tmp/enterprise-v1-backup
docker compose --env-file .env.release.restore -f compose.release.yaml -p enterprise-rag-recovery up -d --wait --no-build
(
  set -a
  . ./.env.release.restore
  set +a
  uv run --locked python tools/release/smoke.py \
    --restore-from /tmp/enterprise-v1-smoke.json --output /tmp/enterprise-v1-restore.json
)
~~~

还原脚本拒绝原项目、损坏快照和已存在的目标数据库。每个数据库在单独事务中恢复，并重设数据库连接权限。恢复验收回读原引用和报告，以原课程资料重新问答。任何一个库恢复失败时，原实例仍保留，可切回原端口，再用新的恢复项目重试。

发版前保留对应版本的镜像与数据库快照。代码回滚应使用适配该快照的版本。若重新创建企业 API 容器，需一起重新创建共享网络命名空间的库存服务。只重新运行初始化挂载不会迁移已有数据卷。
