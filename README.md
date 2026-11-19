# EnterpriseRAG

可信 RAG 核心为本科毕设 **CourseRAG** 和 2027 届春招项目 **EnterpriseAgent** 共用。当前 V0.1 提供本地基础 RAG API，支持中文文本 PDF、UTF-8 Markdown、向量问答、结构化引用和版本化原文读取。两个应用的界面和业务流程在后续版本交付。

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
uv run --locked uvicorn enterprise_rag.api.main:create_app --factory --host 127.0.0.1
~~~

访问本地接口文档 `http://127.0.0.1:8000/docs`。 `/healthz` 只检查 API 进程存活。再次启动 Compose 会保留既有数据。Docker 初始化 SQL 只对空数据卷自动执行。

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

答案的 `evidence[]` 包含 `source_id` 和 `locator`。用 `GET /documents/{source_id}/versions/{version_id}/units/{unit_id}` 加同一令牌读取原文。按 `[start, end)` 切片可复核引用。PDF 示例的 LRU 证据位于物理第 2 页。可用同一上传方式导入 `data/samples/enterprise_inventory.md`，用 `enterprise-demo` 集合与对应令牌询问盘点差异何时登记。两份材料均为本项目编写的公开演示样例，企业信息为合成数据。

- 首次发布索引返回 201。相同文档、原始字节和索引配置重复提交时返回相同的版本和索引 ID，状态码 200。上传时可传已有 `document_id` 为该文档增加修订。旧版本的引用仍能按授权读回。
- 引用由服务端绑定到本次候选证据，`support_status=unchecked` 表示尚未做语义支持性验证。模型无依据或任何陈述缺少可绑定引用时返回 `insufficient_evidence`。依赖故障返回明确的 HTTP 错误和请求 ID。
- 默认每个文件最多 5 MiB，PDF 最多 100 页。保留 pypdf 的文本层，跳过空白或无文字页并报告页码。扫描件、加密 PDF、复杂版式和不可信上传场景超出本地演示范围。

## 测试

使用独立的本地测试数据库。设置 `RAG_TEST_DATABASE_URL` 为已初始化的应用连接串。测试数据通过随机应用 ID 隔离。默认测试不调用模型服务。启用真实模型验收前，确认 .env 的模型配置指向预期的本地或已授权端点。

~~~bash
uv run --locked pytest tests/unit
uv run --locked pytest -m 'not live'
RAG_RUN_LIVE=1 uv run --locked pytest -m live -o log_cli=true -o log_cli_level=INFO
~~~

真实模型验收覆盖 PDF、Markdown、重复导入、原文定位和证据不足。应用日志可通过 Python 日志配置启用 `enterprise_rag` 的 INFO 级别，记录请求 ID、耗时、状态和可得 Token 数。测试记录与限制见 [V0.1 交接](docs/handoff/V0.1.md)。

## 项目文档

- [总体规划](PROJECT_PLAN.md)
- [架构与代码目录](docs/architecture.md)
- [开发路线图与验收门槛](docs/roadmap.md)
- [数据与证据契约](docs/data_design.md)
- [CourseRAG 与 EnterpriseAgent 评测协议](docs/evaluation.md)
- [开源调研与许可证](docs/open_source_references.md)
- [开发与交接规范](docs/development_workflow.md)
