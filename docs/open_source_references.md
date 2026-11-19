# 开源调研与技术选型

核查日期 2026-11-19（Asia/Shanghai）。证据来自官方 API 文档、GitHub 源码与 LICENSE、项目发布记录、PyPI 元数据和 Docker Hub 清单。维护状态以观察到的发布或提交记录为依据。依赖候选通过静态解析，数据库、模型和产品行为的运行验收在各自阶段执行。

## 1. 复用项目、位置与维护

| 项目 | 可复用能力与代码位置 | 适配、依赖、维护与许可证 |
| --- | --- | --- |
| LangChain | 用 `BaseChatModel` / `Embeddings` 契约及供应商包，首个参考适配器为 `ChatOpenAI` 和 `OpenAIEmbeddings`。[模型基类](https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/language_models/chat_models.py) · [Embedding 契约](https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/embeddings/embeddings.py) · [供应商实现](https://github.com/langchain-ai/langchain/tree/master/libs/partners/openai/langchain_openai) | V0.1 采用 Core 与具体供应商包，保留项目自有证据模型，缩小直接依赖。PyPI 有持续更新的 1.x 包，近期 Core 发布 1.8.3。MIT。[许可证](https://github.com/langchain-ai/langchain/blob/master/LICENSE) · [发布](https://github.com/langchain-ai/langchain/releases) |
| LangGraph | V0.5 使用 Graph API 的状态、节点和条件路由，V0.6 使用 PostgreSQL 检查点和受控重试。[StateGraph](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/graph/state.py) · [PostgresSaver](https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint-postgres/langgraph/checkpoint/postgres) | 1.2.23 有公开发布。运行图和 Postgres 检查点分别依赖 `langgraph` 与 `langgraph-checkpoint-postgres`。使用开源 Python 包运行。MIT。[许可证](https://github.com/langchain-ai/langgraph/blob/main/LICENSE) · [发布](https://github.com/langchain-ai/langgraph/releases) |
| pgvector 与 Python 客户端 | 复用 PostgreSQL 扩展的余弦距离 `<=>`、精确检索及后续可选 HNSW。Psycopg 适配器负责注册 vector 类型。[扩展与过滤说明](https://github.com/pgvector/pgvector/tree/v0.8.7) · [向量类型实现](https://github.com/pgvector/pgvector/blob/master/src/vector.c) · [Psycopg 注册实现](https://github.com/pgvector/pgvector-python/blob/master/pgvector/psycopg/register.py) | 扩展 v0.8.7 的文档覆盖 PostgreSQL 13+，本项目用 17。Python `pgvector` 0.5.1 独立发版。索引与授权过滤由项目 SQL 明确控制。扩展 PostgreSQL License，Python 客户端 MIT。[扩展许可](https://github.com/pgvector/pgvector/blob/v0.8.7/LICENSE) · [客户端许可](https://github.com/pgvector/pgvector-python/blob/master/LICENSE.txt) |
| Ragas | V0.3 引入当前 collections 指标 API，先选上下文 Faithfulness 做辅助评测。[指标实现](https://github.com/vibrantlabsai/ragas/blob/main/src/ragas/metrics/collections/faithfulness/metric.py) · [指标 API](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/) | 近期发布 v0.4.4。依赖模型评审、NumPy 及多项 LangChain 包，进入评测依赖组。引用覆盖率、证据跨度和拒答主指标由项目规则及人工标注负责。Apache-2.0。[许可证](https://github.com/vibrantlabsai/ragas/blob/main/LICENSE) · [发布](https://github.com/vibrantlabsai/ragas/releases) |
| MaxKB | 参考文档/段落和知识库版本的组织、embedding 入库与检索适配。[知识模型](https://github.com/1Panel-dev/MaxKB/blob/v2/apps/knowledge/models/knowledge.py) · [Embedding 任务](https://github.com/1Panel-dev/MaxKB/blob/v2/apps/knowledge/task/embedding.py) · [pgvector 适配](https://github.com/1Panel-dev/MaxKB/blob/v2/apps/knowledge/vector/pg_vector.py) | 完整知识库产品依赖 Django 业务与 Web 前端，改造会接管现有模型、权限和 UI。核查到 v2.10.8-lts，公开 v2 分支继续维护。该版本根许可证为 GPLv3，分发派生作品时需评估对应源代码、版权声明等义务。项目采用设计参考方式。[发布](https://github.com/1Panel-dev/MaxKB/releases/tag/v2.10.8-lts) · [版本许可证](https://github.com/1Panel-dev/MaxKB/blob/v2.10.8-lts/LICENSE) |
| Dify | 参考检索参数、工作流知识节点及运行状态展示。[检索服务](https://github.com/langgenius/dify/blob/main/api/core/rag/datasource/retrieval_service.py) · [知识节点](https://github.com/langgenius/dify/blob/main/api/core/workflow/nodes/knowledge_retrieval/knowledge_retrieval_node.py) · [Compose 服务](https://github.com/langgenius/dify/blob/main/docker/docker-compose.yaml) | 近期发布 1.20.0。完整平台包括 Web、API、工作进程、插件、Redis 等。发布许可证基于修改后的 Apache 2.0，源代码多租户运营和使用其前端时的标识/版权有附加条件。本项目采用设计参考方式。[发布](https://github.com/langgenius/dify/releases/tag/1.20.0) · [版本许可证](https://github.com/langgenius/dify/blob/1.20.0/LICENSE) |
| pypdf | V0.1 用 `PdfReader` / `PageObject.extract_text()` 逐页抽文本。[页面实现](https://github.com/py-pdf/pypdf/blob/7.1.1/pypdf/_page.py) · [文本提取能力](https://pypdf.readthedocs.io/en/stable/user/extract-text.html) | 轻量纯 Python 文本层路径，坐标与 OCR 有边界。近期发布 7.1.1，BSD-3-Clause。坐标探索先从 pypdf visitor 和固定夹具开始，若不足再评估 MIT 的 pdfplumber。[pypdf 许可](https://github.com/py-pdf/pypdf/blob/7.1.1/LICENSE) · [pdfplumber 许可](https://github.com/jsvine/pdfplumber/blob/stable/LICENSE.txt) |
| BM25S、jieba | V0.2 分词并建立受 scope 管理的词法索引，支持候选与词典固定。[BM25S 实现](https://github.com/xhluca/bm25s/blob/main/bm25s/__init__.py) · [jieba 源码](https://github.com/fxsjy/jieba/tree/master/jieba) | BM25S 使用 NumPy/SciPy 生态，适于小型语料。观察到 GitHub 0.3.17 与 PyPI 0.3.16 不同步。jieba PyPI 为 0.42.1，GitHub 可见更新节奏较慢，由项目锁定词典并用中文回归夹具覆盖。两者为 MIT。[BM25S 许可](https://github.com/xhluca/bm25s/blob/main/LICENSE) · [jieba 许可](https://github.com/fxsjy/jieba/blob/master/LICENSE) · [BM25S PyPI](https://pypi.org/project/bm25s/) |
| Sentence Transformers | V0.2 对有限候选用 `CrossEncoder.predict`，中文首选验证 `BAAI/bge-reranker-base`。[组件源码](https://github.com/huggingface/sentence-transformers/blob/main/sentence_transformers/cross_encoder/model.py) · [CrossEncoder API](https://sbert.net/docs/cross_encoder/usage/usage.html) | v6.3.0 有近期发布，Torch 与模型权重单独管理，CPU 延迟、内存和中文效果先在开发集测试。库 Apache-2.0，候选权重卡标注 MIT，锁定模型修订并核对分发许可。[库许可](https://github.com/huggingface/sentence-transformers/blob/main/LICENSE) · [模型卡](https://huggingface.co/BAAI/bge-reranker-base) |
| LlamaIndex | 可使用带元数据的节点与切分器实现 RAG 应用，也可参考节点设计。[节点](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/schema.py) · [切分](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/node_parser/text/sentence.py) | 近期发布 v0.14.26，MIT。可作为框架替换选项。与本方案的模型、节点和检索抽象有重叠，采用 LangChain 生态时保持单套 RAG 契约。[许可证](https://github.com/run-llama/llama_index/blob/main/LICENSE) · [发布](https://github.com/run-llama/llama_index/releases) |

外围组件沿用 FastAPI/Pydantic v2、psycopg 3、HTTPX、Streamlit、pytest 和 Docker Compose。FastAPI 的 `UploadFile` 与 multipart 依赖用于导入，psycopg 分离 SQL 和参数，Streamlit 每端单独运行。运行镜像还要核对 psycopg 及二进制分发所捆绑库的许可和声明。[FastAPI 文件上传](https://fastapi.tiangolo.com/tutorial/request-files/) · [Psycopg 参数绑定](https://www.psycopg.org/psycopg3/docs/basic/params.html) · [Psycopg 项目许可](https://github.com/psycopg/psycopg/blob/master/LICENSE.txt) · [Streamlit 运行](https://docs.streamlit.io/develop/concepts/architecture/run-your-app) · [Compose](https://docs.docker.com/compose/)

## 2. 取舍与推荐

| 路径 | 获得的能力 | 代价和结论 |
| --- | --- | --- |
| 直接 Fork MaxKB 或 Dify | 可快速获得生产形态的知识库、管理 UI、工作流 | 要维护上游同步、内部实体、迁移、部署和许可证。将课程实验、判定状态和套件隔离植入整个平台，涉及较多改造 |
| 完整 RAG 框架 + 自建应用 | LlamaIndex 或完整 LangChain 提供高层检索、节点和丰富集成 | 适合快速搭应用。研究关键路径仍需掌握原文坐标和配置版本。采用一个生态即可，组合两套高层抽象会增加排查成本 |
| 轻量应用层 + 包级复用 | 直接复用解析器、模型适配器、数据库、状态图和评测组件 | 项目负责证据契约、权限和任务编排，前期 UI 较少，能在清晰的实现边界内独立验证。**选用此方案** |

首轮从 pypdf、LangChain Core、单一供应商适配器、psycopg/pgvector 和 FastAPI 起步。Agent 直接用 LangGraph Graph API 配合模型工具调用，项目自身维护工具执行、授权与报告溯源。V0.2 和 V0.3 的重排及评测依赖分别引入。

## 3. 版本与兼容性核查

| 范围 | 锁定前候选版本或已核查事实 |
| --- | --- |
| 基础服务 | Python 3.11，FastAPI 0.165.0，Pydantic 2.14.3，pydantic-settings 2.16.3，Uvicorn 0.54.0，python-multipart 0.0.34。[FastAPI 版本策略](https://fastapi.tiangolo.com/deployment/versions/) · [FastAPI 包元数据](https://pypi.org/project/fastapi/0.165.0/) |
| 模型与解析 | langchain-core 1.8.3，langchain-openai 1.8.3（声明 Core >=1.8.2,<2 与 Python >=3.11,<4），pypdf 7.1.1。[Core 包](https://pypi.org/project/langchain-core/1.8.3/) · [OpenAI 集成包](https://pypi.org/project/langchain-openai/1.8.3/) · [LangChain 安装](https://docs.langchain.com/oss/python/langchain/install) |
| 数据库与测试 | PostgreSQL 17，扩展 OCI 镜像候选 `pgvector/pgvector:0.8.7-pg17`，有 linux/amd64 和 linux/arm64。Python `pgvector==0.5.1`、`psycopg[binary]==3.3.9`、`pytest==9.2.0`、`httpx==0.28.1`。扩展与客户端分别核查和锁定。[镜像](https://hub.docker.com/r/pgvector/pgvector/tags?name=0.8.7-pg17) · [Psycopg wheel](https://pypi.org/project/psycopg-binary/3.3.9/) |
| 后续扩展 | LangGraph 1.2.23、Postgres checkpoint 3.2.1、Ragas 0.4.4、Streamlit 1.67.0 是当前调研快照，在引入阶段重新复核。[LangGraph 包](https://pypi.org/project/langgraph/1.2.23/) · [Checkpoint 包](https://pypi.org/project/langgraph-checkpoint-postgres/3.2.1/) · [Ragas 包](https://pypi.org/project/ragas/0.4.4/) · [Streamlit 包](https://pypi.org/project/streamlit/1.67.0/) |

对 V0.1 表内的 12 个直接依赖（含测试）使用 `uv pip compile` 指定 Python 3.11、`--only-binary :all:` 和目标平台，macOS ARM64、Linux x86_64 均解析出 55 个包。以相同基线分别追加 Agent/界面候选组和 Ragas 评测组，两平台也能解析，分别得到 81/82 和 104/104 个包。工作区主解释器为 Python 3.15.2，PATH 上未发现 `python3.11`，可用 uv 置备隔离版本。Docker Engine 与 Compose CLI 可见。本轮的验证范围为包元数据、目标平台解析以及镜像清单。V0.1 首项验收覆盖实际导入、上传 API、`CREATE EXTENSION vector`、向量读写及真实模型调用。

- **Chat/Embedding。** Chat 以 `invoke` 和适配器支持的 `with_structured_output` 组装草稿，Embedding 以 `embed_documents` / `embed_query` 生成同维向量。两种能力分别配置供应商、URL、密钥、模型、版本指纹和超时。以 `langchain-openai` 作标准 API 参考实现。第三方自定义字段或工具响应协议使用对应供应商适配器，经探针验证后采用。若稳定模型只支持普通结构化 JSON，适配器以项目 schema 校验。Citation 始终由后端绑定。[Chat API](https://docs.langchain.com/oss/python/integrations/chat/openai) · [Embedding API](https://docs.langchain.com/oss/python/integrations/text_embedding/openai)
- **LangGraph。** `StateGraph`、条件边、`compile` 和 `invoke` 构成工作流，`bind_tools` 绑定工具描述，模型只提出执行请求。V0.6 使用 checkpointer 和服务端绑定的 `thread_id`，初始化检查点表并明确重试错误类别。[Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api) · [工具调用](https://docs.langchain.com/oss/python/langchain/models) · [持久化](https://docs.langchain.com/oss/python/langgraph/persistence) · [故障处理](https://docs.langchain.com/oss/python/langgraph/fault-tolerance)
- **PostgreSQL。** 按 scope 和发布配置过滤，以 `<=>` 精确排序作为可控基线。观察到数据规模瓶颈再测 HNSW 与过滤的召回影响。企业工具使用独立只读角色、模板和数据库超时。[pgvector 精确与近似检索](https://github.com/pgvector/pgvector/tree/v0.8.7) · [PostgreSQL 会话参数](https://www.postgresql.org/docs/17/runtime-config-client.html)
- **Ragas。** 使用 `ragas.metrics.collections.Faithfulness` 的 `score` / `ascore` 路径。部署可独立锁定评测环境。以已获得的上下文运行辅助打分，关键的引用标注在自有 schema 内。[Faithfulness API](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/) · [RAG 评测教程](https://docs.ragas.io/en/stable/getstarted/rag_eval/)

## 4. 许可与来源登记

通过包依赖复用时，记录包和版本、锁文件、许可证、再分发的镜像/模型与声明。直接复制或改编代码时，逐文件登记上游仓库、路径、版本或 SHA、取得日期、原始版权、许可证/NOTICE、本地位置与修改。分发前核查每个适用子目录及捆绑依赖。Apache 项目需检查 NOTICE 与已修改文件声明，MIT、BSD 和 PostgreSQL 许可需保留对应版权和许可文本。MaxKB 和 Dify 只阅读其公开设计，项目代码自行实现。文档和模型权重有各自的使用与再分发条件，样例另存来源和授权。

## 5. 研究依据

- ALCE 将回答正确性和引用质量分别度量，可参考其可核验引文设计。[Gao 等，EMNLP 2023](https://aclanthology.org/2023.emnlp-main.398/)
- RAG attribution 研究讨论引用正确性与模型忠实性的差别。论文报告可沿用这一概念边界，并对评审口径给出解释。[TU Delft 论文记录](https://research.tudelft.nl/en/publications/correctness-is-not-faithfulness-in-retrieval-augmented-generation/)
- RRF 用于融合各检索分支排名。开发集固定融合常数、候选大小和重排配置，保留可重复的单分支对照。[Cormack 等，SIGIR 2009](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf)
- Ragas 论文提供检索上下文、回答与评审的自动化评测背景。[Es 等，EACL 2024](https://aclanthology.org/2024.eacl-demo.16/)
