# EnterpriseAgent 本地演示

EnterpriseAgent 使用合成的青禾公司业务快照 `qh-2026-06-v1`。分析员可查询销售、库存、仓库制度或三源汇总。制度查阅角色只可查看制度。V0.6 工作台把报告保存在当前令牌与角色的会话中。

V1.0 容器环境请按 [独立部署与恢复指南](deployment.md) 启动，再使用本页的任务和来源回查步骤。

## 配置与启动

1. 按 [主项目启动说明](../README.md#本地启动) 配置 `.env`、PostgreSQL、Chat 和 Embedding。实测使用本地 Ollama 的 `qwen3.5:9b` 与 `nomic-embed-text:latest`。Chat 模型需支持原生工具调用。
2. 分别生成 `ENTERPRISE_DB_READER_PASSWORD`、`ENTERPRISE_ANALYST_TOKEN`、`ENTERPRISE_VIEWER_TOKEN`。将只读密码和数据库端口填入 `ENTERPRISE_READER_URL`。制度索引默认使用 `RAG_DATABASE_URL`，可用 `ENTERPRISE_RAG_DATABASE_URL` 指向独立的已迁移数据库。
3. 运行 `docker compose up -d --wait`。空数据卷会自动加载合成销售快照和只读账号。已有数据卷应手动应用迁移。下列命令从 `.env` 中读取配置，数据库必须是本地演示实例。再次执行会幂等补齐快照并设置指定的只读账号密码。

~~~bash
set -a
. ./.env
set +a
docker compose exec -e ENTERPRISE_DB_READER_PASSWORD -T db psql -v ON_ERROR_STOP=1 -U rag_owner -d enterprise_rag < migrations/enterprise_agent/005_business_snapshot.sql
~~~

从 V0.5 升级时，对已有数据卷应用会话迁移，再启动企业 API。空数据卷会自动应用 `006_agent_state.sql`。企业 API 首次启动会在专用 schema 初始化 LangGraph 检查点表。独立的 `ENTERPRISE_RAG_DATABASE_URL` 也需要应用此迁移。

~~~bash
docker compose exec -T db psql -v ON_ERROR_STOP=1 -U rag_owner -d enterprise_rag < migrations/enterprise_agent/006_agent_state.sql
~~~

在仓库根目录开三个终端，分别启动库存 HTTP 服务、业务 API 和 Streamlit。若启用了本地 BGE 重排，在三条 `uv run` 命令都加 `--extra rerank`。

~~~bash
uv run --locked --extra agent uvicorn enterprise_rag.apps.enterprise_agent.tools.inventory:create_inventory_app --factory --host 127.0.0.1 --port 8003
~~~

~~~bash
uv run --locked --extra agent uvicorn enterprise_rag.apps.enterprise_agent.api:create_enterprise_app --factory --host 127.0.0.1 --port 8002
~~~

~~~bash
uv run --locked --extra agent streamlit run src/enterprise_rag/apps/enterprise_agent/ui.py --server.address 127.0.0.1 --server.port 8502
~~~

## 演示与回查

1. 打开 [业务工作台](http://127.0.0.1:8502)，输入分析员令牌，点击「载入演示制度」。
2. 选择「业务概览」，使用默认的 2026-06-01、华东、A100，生成报告。固定销售数据为 2 笔订单、12 件、¥1,450.00。A100 在库 18 件、预留 5 件、可用 13 件。制度证据可回读「须在 24 小时内登记差异」及其原文偏移。
3. 展开来源和工具调用记录，核对 SQL 模板与参数、库存 HTTP 返回字段、制度文档版本，以及四个角色的运行顺序。刷新页面后可重新打开保存的会话。换用制度查阅令牌，销售报告与分析员的会话入口都不可见。
4. 若 API 进程在任务运行中退出，重启服务，再用原令牌打开原会话，选择未完成记录并点击「继续任务」。已提交的成功工具结果会复用。任务原有的模型、工具和时间预算仍然有效。

接口文档在 [本地 API 文档](http://127.0.0.1:8002/docs)。`POST /agent/tasks` 保留 V0.5 单智能体。V0.6 先用 `POST /agent/threads` 创建会话，再用 `PUT /agent/threads/{thread_id}/runs/{run_id}` 提交任务。客户端生成 UUID 作为运行 ID，相同任务重复提交时返回同一报告。发生中断可调用 `POST /agent/threads/{thread_id}/runs/{run_id}/resume`。查询历史使用 `GET /agent/threads/{thread_id}/runs`。两种入口都接受 `overview`、`sales`、`inventory` 与 `policy` 四种任务。

会话访问同时依赖原角色和原令牌，更换令牌后历史会话不再可访问。V0.6 保留最近三条同会话任务概要，供角色理解上下文，任务的显式日期、地区和商品继续决定实际工具范围。工作流只重试明确的暂时性错误。依赖故障时显示已核验的证据及缺失来源。

## 复现 E0/E1

运行时需已启动 PostgreSQL、本机 Ollama 和库存 HTTP 服务。评测自行启动两个隔离的 ASGI 应用，共用同一任务、模型、预算和合成快照。E0 只开放 SQL/API，E1 同时开放制度检索。也可在两条命令加 `--extra rerank` 复现实测配置。

~~~bash
uv run --locked --extra agent python -m enterprise_rag.apps.enterprise_agent.evaluate run --run-id enterprise-dev-local
uv run --locked --extra agent python -m enterprise_rag.apps.enterprise_agent.evaluate summarize runs/enterprise/enterprise-dev-local
~~~

评测保存配置、逐例响应、来源回查结果、指标和失败说明。授权阻断单列。没有模型用量的样本保留为缺失值。常规测试使用独立的测试数据库，分别设置 `RAG_TEST_DATABASE_URL` 与 `ENTERPRISE_TEST_READER_URL`。[案例与判分依据](../data/enterprise_agent/evaluation/v0.5.0/cases.json)

## 复现 E1/E2 与故障对照

~~~bash
uv run --locked --extra agent python -m enterprise_rag.apps.enterprise_agent.evaluate_reliability run --run-id enterprise-reliability-local
uv run --locked --extra agent python -m enterprise_rag.apps.enterprise_agent.evaluate_reliability summarize runs/enterprise/enterprise-reliability-local
~~~

固定故障包含一次库存超时和始终返回无效数据。关闭重试组仅执行两条故障任务。运行真实模型时仍由本机库存服务提供正常结果。[故障清单](../data/enterprise_agent/evaluation/v0.6.0/faults.json)
