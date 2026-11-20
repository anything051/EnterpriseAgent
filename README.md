# EnterpriseRAG

可信 RAG 核心为本科毕设 **CourseRAG** 和 2027 届春招项目 **EnterpriseAgent** 共用。两个应用有各自的入口、作用域、演示数据和评测。

- **CourseRAG V0.4** 提供课程目录、材料版本、可信问答、PDF 原文高亮及开发评测归档导出。[CourseRAG 启动与演示](docs/course_rag_demo.md)
- **EnterpriseAgent V0.5** 使用 LangGraph、真实工具调用、只读销售查询、库存 HTTP API 和仓库制度检索，生成可回查的业务报告。[EnterpriseAgent 启动与演示](docs/enterprise_agent_demo.md)

共享 API 支持中文文本 PDF、UTF-8 Markdown、混合检索、可选本地重排、结构化引用和证据校验。

## 本地启动

需要 Python 3.11、[uv](https://docs.astral.sh/uv/) 和 Docker Compose。使用 Ollama 时准备本地 Chat 与 Embedding 模型。也可分别将任一能力配置为 OpenAI API。

~~~bash
uv python install 3.11
uv sync --locked
test -f .env || cp .env.example .env
~~~

编辑本地 .env。分别用 `openssl rand -hex 24` 生成数据库管理密码、应用密码和每个集合的访问令牌。将应用密码同步填入 `RAG_DATABASE_URL`。如果更改数据库端口，同时更新连接串端口。Compose 将 PostgreSQL 暴露在本机回环地址，业务连接使用最小权限的 `rag_app`，首次启动的空数据卷自动应用迁移。

本地 Ollama 示例经 `qwen3.5:9b` 和 `nomic-embed-text:latest` 验证。若模型尚未下载，先按 [Ollama 文档](https://docs.ollama.com/) 安装并启动 Ollama，再运行下列命令。9B 模型需要相应的内存和磁盘空间。选择其他 Chat 模型时，先确认它能返回严格的 JSON。

~~~bash
ollama pull qwen3.5:9b
ollama pull nomic-embed-text
curl -fsS http://127.0.0.1:11434/api/tags
~~~

将 Embedding 模型的完整 digest 填入 `RAG_EMBEDDING_REVISION`。按所选模型设置维度与检索前缀。更换 Embedding 模型、修订、维度、解析器或切分配置时，使用新集合重新导入资料。V0.1 暂无在线索引切换命令。OpenAI 配置填写独立的供应商、模型、URL 和 Key，Embedding 维度须与模型实际输出一致。向云模型发送的内容限于已授权资料。

~~~bash
docker compose up -d --wait
~~~

已有 V0.1 数据卷时，数据库启动后执行一次兼容迁移。此 SQL 可重复执行，已有文档、索引 ID 与向量继续可用。新数据卷会自动执行。

~~~bash
docker compose exec -T db psql -v ON_ERROR_STOP=1 -U rag_owner -d enterprise_rag < migrations/core/003_highlight_source.sql
~~~

~~~bash
uv run --locked uvicorn enterprise_rag.api.main:create_app --factory --host 127.0.0.1
~~~

访问本地接口文档 `http://127.0.0.1:8000/docs`。 `/healthz` 只检查 API 进程存活。再次启动 Compose 会保留既有数据。Docker 初始化 SQL 只对空数据卷自动执行。

## 检索、重排与证据策略

默认 `RAG_RETRIEVAL_MODE=hybrid` 和 `RAG_VALIDATION_MODE=gate`。中文 BM25S 与 pgvector 在同一发布快照内各取最多 20 条，使用 RRF 融合。语义审阅检查每条引用、组合支撑、其他候选中的冲突，并单独判断问题是否回答完整。没有可绑定引用的陈述会触发统一拒答。研究用的 `annotate` 保留有引用的草稿和判定，`off` 仅做基础引用绑定。设为 `vector` 和 `off` 可复现 V0.1 基线。

可选重排为固定版本的 BGE CrossEncoder INT8，在 CPU 上运行。若本地目录还没有模型文件，可按以下命令下载。可跳过下载直接使用混合检索与 RRF。运行时会校验两个文件的 SHA-256，重排超时或不可用时响应会标记实际使用的策略。

~~~bash
base=https://huggingface.co/Xenova/bge-reranker-base/resolve/280bcc27a84e0b898c251e06fddb25171bd9b101
mkdir -p .rag-models/bge
curl -fL "$base/tokenizer.json" -o .rag-models/bge/tokenizer.json
curl -fL "$base/onnx/model_int8.onnx" -o .rag-models/bge/model_int8.onnx
uv sync --locked --extra rerank
RAG_RERANKER_DIR=.rag-models/bge uv run --locked --extra rerank uvicorn enterprise_rag.api.main:create_app --factory --host 127.0.0.1
~~~

## API 演示

以下调用使用 .env 中 `course-demo` 的令牌。将其读入本地环境变量 `RAG_BEARER_TOKEN`，例如在 Bash 或 Zsh 中运行 `read -rs RAG_BEARER_TOKEN` 后输入令牌。

~~~bash
curl -fsS http://127.0.0.1:8000/healthz

curl -fsS http://127.0.0.1:8000/documents \
  -H "Authorization: Bearer $RAG_BEARER_TOKEN" \
  -F collection_id=course-demo \
  -F file=@data/samples/course_memory.pdf

curl -fsS http://127.0.0.1:8000/questions \
  -H "Authorization: Bearer $RAG_BEARER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"collection_id":"course-demo","question":"根据课程笔记，LRU 选择哪个页面？","k":5}'
~~~

答案的 `retrieval` 记录实际检索策略、候选 ID 和重排状态。开启证据审阅时，`verification` 记录每条陈述的判定、问题覆盖状态和可回读的冲突证据。答案的 `evidence[]` 包含 `source_id` 和 `locator`。用 `GET /documents/{source_id}/versions/{version_id}/units/{unit_id}` 加同一令牌读取原文。按 `[start, end)` 切片可复核引用。可在该路径后追加 `/highlight?start=…&end=…` 获取 PDF 字符框。优先使用引用的 `quote_span`，否则使用证据的 Chunk 范围。PDF 示例的 LRU 证据位于物理第 2 页。可用同一上传方式导入 `data/samples/enterprise_inventory.md`，用 `enterprise-demo` 集合与对应令牌询问盘点差异何时登记。两份材料均为本项目编写的公开演示样例，企业信息为合成数据。

- 首次发布索引返回 201。相同文档、原始字节和索引配置重复提交时返回相同的版本和索引 ID，状态码 200。上传时可传已有 `document_id` 为该文档增加修订。旧版本的引用仍能按授权读回。
- 引用由服务端绑定到本次候选证据。`support_status` 表示单条引用的支持性，组合结论在 `verification.claims` 中。`gate` 模式对证据不足、冲突或回答不完整返回 `insufficient_evidence`，并提供 `refusal_reason`。依赖故障返回 HTTP 错误和请求 ID。
- 默认每个文件最多 5 MiB，PDF 最多 100 页。保留 pypdf 的文本层，跳过空白或无文字页并报告页码。扫描件、加密 PDF 和不可信上传场景超出本地演示范围。页面旋转、几何异常或原文对齐不一致时返回 `page_only`，Markdown 返回 `text_only`。

## V0.3 对照评测

开发集有 10 道自编题与 4 份课程材料。运行器为每轮创建独立数据库作用域，B/C/D 共享混合检索、排序和生成。C 加审阅，D 加拒答。使用已启动的 PostgreSQL、声明在 .env 的本地模型和已核验的 BGE 文件。

~~~bash
run_id="course-dev-$(date -u +%Y%m%dT%H%M%SZ)"
RAG_RERANKER_DIR=.rag-models/bge uv run --locked --extra rerank python -m enterprise_rag.evaluation run --run-id "$run_id" --seed 42
uv run --locked python -m enterprise_rag.evaluation summarize "runs/$run_id"
~~~

第一条命令保存配置、逐例记录、指标和失败说明。若任一组遇到模型、数据库或重排故障，运行器仍保存各组结果并以非零状态退出。第二条命令无需数据库和模型即可重算。公开开发集的实际结果见 [A–D 报告](docs/evaluation_runs/course-dev-20261120/report.md)。该结果用于发现工程缺陷，正式毕设应另建独立测试集并进行人工复核。

可选 Ragas 在独立环境中复核既有回答，记录附加审阅用量。该命令只接受本机 Ollama，评分单列。运行前用实际目录替换示例中的 `runs/<运行ID>`。

~~~bash
uv run --locked --project tools/ragas python tools/ragas/faithfulness.py --run 'runs/<运行ID>' --group B --question os-lru --output 'runs/<运行ID>/ragas.json'
~~~

## 容器模式

默认 Compose 只启动数据库。`api` profile 可同时启动容器化 API。若之前已在宿主机使用模型地址索引数据，可为容器使用独立 `RAG_APP_ID` 并重新导入对应资料。Embedding 的访问地址参与索引指纹。

~~~bash
RAG_APP_ID=enterprise-rag-container docker compose --profile api up -d --build
~~~

容器默认使用 `host.docker.internal:11434`。在 Linux 主机上，Ollama 必须能从容器访问。可显式设置 `RAG_CONTAINER_CHAT_BASE_URL` 和 `RAG_CONTAINER_EMBEDDING_BASE_URL`。API 端口只在宿主机回环发布。容器基础镜像用于向量或 RRF 路径，想启用 ONNX 重排时采用上述宿主机安装方式。

## 测试

使用独立的本地测试数据库。设置 `RAG_TEST_DATABASE_URL` 为已初始化的应用连接串。测试数据通过随机应用 ID 隔离。默认测试不调用模型服务。启用真实模型验收前，确认 .env 的模型配置指向预期的本地或已授权端点。

~~~bash
uv run --locked pytest tests/unit
uv run --locked --extra course --extra agent pytest -m 'not live'
RAG_RUN_LIVE=1 RAG_RERANKER_DIR=.rag-models/bge uv run --locked --extra rerank pytest -m live -o log_cli=true -o log_cli_level=INFO
~~~

真实模型回归覆盖基础问答、混合重排、PDF 坐标、组合证据、冲突和问题覆盖。CourseRAG 另有数据库及界面回归。应用日志可通过 Python 日志配置启用 `enterprise_rag` 的 INFO 级别，记录请求 ID、耗时、状态和可得 Token 数。[V0.4 交接与实测](docs/handoff/V0.4.md)

## 项目文档

- [总体规划](PROJECT_PLAN.md)
- [架构与代码目录](docs/architecture.md)
- [开发路线图与验收门槛](docs/roadmap.md)
- [数据与证据契约](docs/data_design.md)
- [CourseRAG 与 EnterpriseAgent 评测协议](docs/evaluation.md)
- [开源调研与许可证](docs/open_source_references.md)
- [开发与交接规范](docs/development_workflow.md)
