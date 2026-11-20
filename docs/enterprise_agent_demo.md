# EnterpriseAgent 本地演示

V0.5 使用合成的青禾公司业务快照 `qh-2026-06-v1`。分析员可查询销售、库存、仓库制度或三源汇总。制度查阅角色只可查看制度。所有结果均随报告记录来源和对应工具调用。

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
3. 展开来源和工具调用记录，核对 SQL 的查询模板、绑定参数与快照，库存 HTTP 的返回字段，以及制度的文档版本和行号。换用制度查阅令牌，界面只显示制度任务。

接口文档在 [本地 API 文档](http://127.0.0.1:8002/docs)。`POST /agent/tasks` 使用结构化任务范围与自然语言描述，`kind` 支持 `overview`、`sales`、`inventory`、`policy`。销售和库存的可用日期、地区、商品以固定演示快照为准。报告只采用已配置模板和校验后的来源。遇到依赖错误时，返回已核实的部分结果、缺失来源和审计。

## 复现 E0/E1

运行时需已启动 PostgreSQL、本机 Ollama 和库存 HTTP 服务。评测自行启动两个隔离的 ASGI 应用，共用同一任务、模型、预算和合成快照。E0 只开放 SQL/API，E1 同时开放制度检索。也可在两条命令加 `--extra rerank` 复现实测配置。

~~~bash
uv run --locked --extra agent python -m enterprise_rag.apps.enterprise_agent.evaluate run --run-id enterprise-dev-local
uv run --locked --extra agent python -m enterprise_rag.apps.enterprise_agent.evaluate summarize runs/enterprise/enterprise-dev-local
~~~

评测保存配置、逐例响应、来源回查结果、指标和失败说明。授权阻断单列。没有模型用量的样本保留为缺失值。常规测试使用独立的测试数据库，分别设置 `RAG_TEST_DATABASE_URL` 与 `ENTERPRISE_TEST_READER_URL`。[案例与判分依据](../data/enterprise_agent/evaluation/v0.5.0/cases.json)
