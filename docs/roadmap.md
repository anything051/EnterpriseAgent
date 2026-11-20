# 从 V0.0 到 V1.0 的开发路线和验收

本文件中的命令、代码路径与通过条件用于相应阶段。V0.0 在 Markdown 中交付规划，后续阶段按验收记录发布。

~~~mermaid
flowchart LR
    A["V0.0 设计"] --> B["V0.1 基础 RAG"]
    B --> C["V0.2 Trusted RAG"]
    C --> D["V0.3 评测和工程化"]
    D --> E["V0.4 CourseRAG"]
    D --> F["V0.5 EnterpriseAgent"]
    F --> G["V0.6 多智能体与可靠性"]
    E --> H["V1.0 双应用交付"]
    G --> H
~~~

共享核心 V0.0–V0.3 为 P0。V0.4 和 V0.5 均可在 V0.3 后开始，求职窗口紧时先做 V0.5。学校节点由 CourseRAG 单独跟踪。并行开发期间新增应用功能优先在相应应用层完成。核心契约变更需共同评审、迁移及通过共享回归。V0.6 以 V0.5 稳定闭环为前提。V1.0 汇总两端，并保留各自的验收记录。

## V0.0 总体设计

| 项目 | 内容 |
| --- | --- |
| 目标与边界 | 核对仓库、技术与许可，交付六份设计文档。代码、部署和评测执行归后续阶段 |
| 前置 | 当前仓库、项目需求、可获取官方资料 |
| 模块 | 系统边界、证据契约、技术选型、实验设计、开发顺序 |
| 复用与自研 | 参考官方 API、公开源码与论文。自行定义项目范围、数据模型、实验协议和验收 |
| 交付文件 | `PROJECT_PLAN.md`、`docs/architecture.md`、`docs/roadmap.md`、`docs/data_design.md`、`docs/evaluation.md`、`docs/open_source_references.md` |
| 自动化检查 | `git diff --check`、文档相对链接、示例 JSON 和 Mermaid 检查、依赖元数据解析 |
| 验收 | 六份文档可导航。八个阶段均有明确交付与验收。锁版本前的运行验证、许可限制和当前环境有记录 |
| 风险与处理 | 文档和包版本可能不同步。保存查询依据和版本，V0.1 先复核后锁定 |

## V0.1 基础 RAG 引擎

| 项目 | 内容 |
| --- | --- |
| 目标与边界 | 在本地完成文本 PDF/Markdown → 向量 → 回答 → 基础引用 → 原文读取。空候选或无合法引用时返回证据不足 |
| 前置 | V0.0 契约与候选选型。启动时配置 Python 3.11、模型和 Compose 数据库 |
| 模块 | 文档解析、带位置分块、模型适配、数据库迁移、精确向量检索、生成、引用绑定、FastAPI |
| 复用与自研 | 复用 pypdf、LangChain Core/供应商包、psycopg、pgvector、FastAPI。自研切片映射、作用域过滤、发布事务、有效证据绑定 |
| 交付文件 | `pyproject.toml`、`uv.lock`、`.env.example`、`compose.yaml`、`migrations/core/001_initial.sql`、`src/enterprise_rag/{core,adapters,api}/`、`data/samples/`、`tests/{unit,integration}/`、README 启动指南 |
| 自动化测试 | 中文与多页定位、Markdown 段落、无效/纯扫描文件、重复与并发导入、向量维度、模型异常、旧索引保留、稳定排序、无效引用和跨作用域访问 |
| 验收 | 启动数据库并执行迁移后，`uv run --locked pytest tests/unit tests/integration` 通过。使用真实 Chat/Embedding 导入两类材料、提问并定位证据，保存脱敏的运行记录。重复导入的版本和 Chunk 数不变，空集合返回明确状态 |
| 风险与处理 | 无 Python 3.11 或真实模型处于不可用状态时先用隔离环境和模型替身运行。真实探针仍是阶段验收项。模型能力、连接、超时、上传和维度问题要能分别定位 |

### V0.1 实现任务清单

1. **环境与依赖探针。** 获取 Python 3.11，使用 [已核查候选](open_source_references.md#3-版本与兼容性核查) 生成锁文件，验证依赖导入和 FastAPI 测试客户端。配置 Chat/Embedding 的供应商、模型、独立凭据、地址、超时与维度，禁用默认外部追踪。一次真实 Chat 结构化输出、一次真实 Embedding 和一次 PostgreSQL 扩展查询作为能力探针。
2. **样例与解析。** 在 `data/samples/` 编写可公开分发的中文 PDF 和 Markdown 小样本，带已知页码、标题、段落与问题。实现 `core/documents.py`，从原文单元保留边界并切分。对片段等于原文切片做断言。输入限制、无法解析页和无可用文本有可见结果。
3. **索引与存储。** 在 `migrations/core/001_initial.sql` 定义逻辑文档、不可变版本、原文单元、Chunk、索引配置与向量。配置 Compose 本地服务和最小权限。实现 `adapters/postgres.py` 的原子发布、幂等重试和作用域约束。
4. **Embedding 与检索。** 在 `core/embeddings.py` 和 `core/retrieval.py` 实现批量写入、向量与配置校验、按 scope 的精确余弦 Top-K。同分按 Chunk ID 排序。用已知距离的测试向量验证数据库结果。
5. **生成与引用。** 在 `core/generation.py` 与 `core/citations.py` 传入本次证据和候选 ID，校验结构、作用域、ID 和原文切片。返回分离的答案、引用和证据，合法引用标记为 `unchecked`。区分模型故障和证据不足。
6. **API 与复现。** 实现 [V0.1 路由](architecture.md#3-v01-服务契约)，写入测试和 README 使用说明。以授权身份导入、问答、读取原文，再验证新版本、重复导入、无证据、越权和失败后重试。记录真实模型、配置、测试命令和结果。

V0.1 保持精确向量检索与基础引用，BM25、Rerank、语义校验、LangGraph 和演示界面按后续版本进入。仓库已有一份 [V0.1 提示词](CODEX_V0_1_PROMPT.md)，具体模块与验收细则以本节及关联契约为准。

## V0.2 Trusted RAG

| 项目 | 内容 |
| --- | --- |
| 目标与边界 | 中文 BM25 + 向量融合、重排、细粒度原文定位、声明支持校验和证据不足处理 |
| 前置 | V0.1 稳定快照、来源偏移、基础检索与答案 |
| 模块 | 词法索引、RRF、CrossEncoder、原文跨度解析、声明级验证器、拒答策略 |
| 复用与自研 | 复用 BM25S、jieba、BGE CrossEncoder ONNX、pdfplumber。自研同 scope 候选合并、支持校验输入/输出、引用组合与拒答策略 |
| 交付文件 | `src/enterprise_rag/core/{retrieval,reranking,citations,validation}.py`、分词与排序配置、定位/冲突样例、对应 `tests/` |
| 自动化测试 | 分词和 RRF 确定性、重排超时降级、坐标与页码回退、单证据及组合支持、语义冲突、引用缺失 |
| 验收 | 用固定样例运行 `uv run --locked pytest tests/unit tests/integration`。检查包含精确术语、跨段、无证据和冲突四类结果。重排失败时可见实际分支。可获得可靠坐标的样例可复核高亮，其他样例可按页定位 |
| 风险与处理 | 中文分词、解析坐标和自动评审存在误差。冻结词典与模型版本，使用页码回退，阈值只在开发集拟合并保留人工判定 |

## V0.3 评测和基础工程化

| 项目 | 内容 |
| --- | --- |
| 目标与边界 | 实现离线运行器、课程 A–D 实验与固定数据/评分，建立 CI 与基础容器运行 |
| 前置 | V0.2 的 A–D 能力和可追踪状态 |
| 模块 | 样本结构、运行配置、聚合器、Ragas 适配、失败分析、CI |
| 复用与自研 | 复用 pytest、Ragas、Docker Compose 和 GitHub Actions。自行定义 gold evidence、引用与拒答口径并生成逐例结果 |
| 交付文件 | `src/enterprise_rag/evaluation/`、`data/course_rag/evaluation/`、`.github/workflows/ci.yml`、更新后的 `compose.yaml`、真实运行报告和产物清单 |
| 自动化测试 | 手算评测夹具、零分母、异常/缺失、复跑聚合、离线模型替身和数据库回归 |
| 验收 | `uv run --locked pytest` 通过。按 README 中实现的 CLI 录入版本化测试集，完成 A–D 真实运行。可由逐例文件重算聚合指标，记录模型、锁文件、数据、环境和限制 |
| 风险与处理 | 成本与模型漂移影响比较。先跑小开发集，预算后锁定保留集，盲检代表样本并记录价格日期、用量缺失与失败 |

## V0.4 CourseRAG 应用

| 项目 | 内容 |
| --- | --- |
| 目标与边界 | 课程/章节/知识点及材料管理、课程问答、可点击引用和独立毕设演示 |
| 前置 | V0.3 的稳定共享契约、评测工具和课程语料 |
| 模块 | 课程授权与元数据、独立 FastAPI/Streamlit 入口、PDF 查看和评测导出 |
| 复用与自研 | 复用 Streamlit、FastAPI、共享核心的检索与证据接口。自研章节、知识点、材料版本发布和课程演示 |
| 交付文件 | `src/enterprise_rag/apps/course_rag/`、`migrations/course_rag/`、`data/course_rag/`、`tests/e2e/test_course_rag.py`、课程演示指南及毕设实验记录 |
| 自动化测试 | 章节与材料关联、课程间隔离、版本发布、引用重开与 PDF 页级回退、评测导出 |
| 验收 | 按课程指南独立启动。创建两个课程、管理章节并导入材料，完成带定位的问答和一次拒答。切换课程时验证隔离。运行课程 E2E 并产出可复查实验记录 |
| 风险与处理 | 语料授权和学校进度。固定可分发样例，优先完成课程侧核心流程和评测，额外课程可后增 |

## V0.5 EnterpriseAgent 应用

| 项目 | 内容 |
| --- | --- |
| 目标与边界 | LangGraph 单 Agent、真实 Function Calling、知识查询、只读 SQL/API、跨来源业务报告和独立演示 |
| 前置 | V0.3 的共享核心与评测基线，可与 V0.4 并行 |
| 模块 | `StateGraph`、类型化工具、工具授权、合成业务快照、报告与证据映射 |
| 复用与自研 | 复用 LangGraph、模型 `bind_tools`、psycopg、HTTPX、Streamlit 和核心检索。自研工具目录、限额、模板、来源验证与报告逻辑 |
| 交付文件 | `src/enterprise_rag/apps/enterprise_agent/{agents,tools}/`、企业 API/Streamlit 入口、`migrations/enterprise_agent/`、`data/enterprise_agent/`、`tests/e2e/test_enterprise_agent.py`、企业演示指南与 E0/E1 运行记录 |
| 自动化测试 | Function schema、调用预算、白名单及绑定参数、只读账号、错误响应/超时、拒绝未授权来源、SQL/API 结果复算 |
| 验收 | 按企业指南独立启动。在真实支持工具调用的模型上执行从销售与库存查询到制度引用和报告的链路，覆盖 SQL 与 API。E2E 拒绝未授权访问并返回审计记录。用固定样本产出真实 E0/E1 记录 |
| 风险与处理 | 模型缺少工具协议或误用工具。先验证调用及返回 tool-call ID，服务端执行鉴权、校验、模板和预算。无法完成时保留可核查的部分报告 |

## V0.6 多智能体与可靠性

| 项目 | 内容 |
| --- | --- |
| 目标与边界 | 四角色协作、线程级会话持久化、可恢复重试和执行对照 |
| 前置 | V0.5 已完成单 Agent 与安全工具闭环 |
| 模块 | Supervisor、Knowledge/Data/Report Agent、检查点、线程授权、执行键、恢复与资源预算 |
| 复用与自研 | 复用 LangGraph 状态与 PostgresSaver，按需使用 RetryPolicy。自研角色路由、状态迁移、重放去重和故障评测 |
| 交付文件 | `src/enterprise_rag/apps/enterprise_agent/agents/` 四角色与状态实现、检查点迁移、`tests/integration/test_agent_resume.py`、E1/E2 及策略对照报告 |
| 自动化测试 | 瞬时失败、永久失败、重启恢复、重复执行、线程串扰、预算终止、部分结果 |
| 验收 | 在指定任务中断后重新启动进程，以同一线程恢复并验证已完成工具结果受控复用。固定故障脚本测 E1/E2，交付实际成功率、延迟与 Token，无胜出要求 |
| 风险与处理 | 多 Agent 累积成本，状态结构升级与重复重试。限制节点和步数，版本化检查点状态，保留单 Agent 基线方案 |

## V1.0 双应用工程交付

| 项目 | 内容 |
| --- | --- |
| 目标与边界 | 安全强化、双应用端到端评测、GitHub 文档、容器运行和演示 |
| 前置 | V0.4 的课程端、V0.6 的企业端和已归档评测工具 |
| 模块 | 跨应用授权和隔离回归、运行日志、部署参数、备份恢复、演示与发布文档 |
| 复用与自研 | 复用 FastAPI 安全组件、Compose/CI、pytest 与离线评测运行器。自行收敛生产配置、权限策略、运行手册与结果说明 |
| 交付文件 | `README.md`、`docs/deployment.md`、独立演示指南、Compose 配置、`tests/e2e/`、依赖与第三方声明、两端真实评测报告及发布记录 |
| 自动化测试 | 端到端问答/来源/报告、跨作用域与线程隔离、凭据和日志脱敏、只读工具、回滚和恢复演练 |
| 验收 | 在干净环境依据文档运行 `docker compose config`、容器启动与两端 E2E。各应用可用其独立数据集和入口演示。评测报告可回算，发布包含锁文件及合规来源记录 |
| 风险与处理 | 平台与环境差异、配置泄密、版本迁移失败。锁镜像和依赖，验证干净环境，独立配置开发/生产凭据，演练备份和恢复 |
