# EnterpriseRAG 架构与接口

本文件定义 V0.0 的目标架构和阶段性契约。V0.1 先交付共享核心的最短可运行链路，版本较晚的组件由路线图逐步引入。

## 1. 模块分层

~~~mermaid
flowchart LR
    subgraph Course["CourseRAG · V0.4"]
        CUI["课程 Streamlit"] --> CAPI["课程 API 与课程/章节/知识点服务"]
    end
    subgraph Enterprise["EnterpriseAgent · V0.5–V0.6"]
        EUI["企业 Streamlit"] --> EAPI["企业 API 与授权"]
        EAPI --> AG["LangGraph 状态与报告编排"]
        AG --> KT["知识工具"]
        AG --> DT["只读 SQL / 白名单 API 工具"]
        DT --> BUSINESS[("合成业务数据库 / API")]
        DT --> SE["SQL/API 证据适配"]
    end
    subgraph Core["Trusted RAG Core · V0.1–V0.3"]
        ING["解析 / 切分 / Embedding"] --> STORE[("按作用域隔离的 PostgreSQL + pgvector")]
        QA["共享问答流程"] --> RET["向量 / BM25 / 融合 / Rerank"]
        RET --> STORE
        QA --> GEN["草稿生成"]
        QA --> CITE["引用绑定 / 支持校验 / 拒答"]
        CITE --> SRC[("版本化原文 / 证据")]
        ING --> SRC
    end
    CAPI --> ING
    CAPI --> QA
    KT --> RET
    AG --> CITE
    SE -. "来源校验" .-> CITE
    EVAL["通用评测运行器"] -. "课程评测" .-> CAPI
    EVAL -. "企业评测" .-> AG
~~~

V0.1 由最小 FastAPI 入口组合核心。V0.4 和 V0.5 分别用自己的入口组合核心与业务服务。CourseRAG 的问答会调用共享生成流程。EnterpriseAgent 的知识工具通过共享检索直接取得文档证据，任务报告由业务流程生成后交给共享引用绑定，并按来源校验。图中的核心存储代表相同的实现和数据契约，各应用使用各自有授权的数据与连接。

| 组件 | 公共职责 | 归属和引入阶段 |
| --- | --- | --- |
| Document Parser / Chunk Manager | 解析成版本化原文单元，从原文切片并记录位置，生成可重复的 Chunk | 核心 V0.1，PDF 坐标适配 V0.2 |
| Embedding Service / Vector Store | 批量生成向量，校验维度，按作用域和发布索引读写 | 核心 V0.1 |
| Retriever / Reranker | 精确向量 Top-K，随后加入中文 BM25、RRF、CrossEncoder | 核心 V0.1 / V0.2 |
| Generator / Citation Manager | 生成结构化草稿，后端绑定本次证据并查回原文 | 核心 V0.1 |
| Evidence Validator | 文档跨度解析、事实支持判定和拒答，通过来源类型接入其他验证器 | 文档核心 V0.2，SQL/API 验证器在企业应用 V0.5 |
| Evaluation | 运行、追踪和聚合常用指标，各应用定义任务、gold labels 和业务评分 | 通用运行器 V0.3 |
| 业务应用 | 课程权限和教学流程，以及企业工具授权、编排、会话和报告 | 分别在 V0.4、V0.5、V0.6 |

依赖方向为入口 → 应用服务 → 核心服务 → 已配置的持久化与模型适配器。共享代码只认识 scope、查询、来源、证据和通用运行信息。HTTP 路由、Agent state、课程实体和企业 SQL 模板归对应入口或应用。先实现 Chat、Embedding、存储的必要边界。V0.2/V0.5 到达各自的集成点时再加入重排和业务证据适配。

## 2. 核心数据流

**入库。** 服务端解析身份和目标 scope。接收文件后生成哈希和来源位置，按固定策略提取 PDF 页或 Markdown 段落，切出可反查的文本，批量生成向量并校验维度。在完整索引提交前，检索只读取上一个可用版本。重复导入复用已完成的结果，解析或 Embedding 失败允许受控重试。版本身份、冲突和发布语义见 [数据设计](data_design.md)。

**问答。** 以服务端 scope 和同一索引快照查询。V0.1 使用精确余弦排序。V0.2 各检索器先应用同一 scope，分别取候选，按 Chunk ID 去重，用 RRF 合并，再对有限候选做重排。候选的首选开发配置为各分支 Top-20、RRF 常数 60、融合后重排 20 条。以 `1 / (60 + rank)` 汇总各分支排名，按 Chunk ID 打破并列，最终 K 默认 5。参数在开发集校准后随评测冻结。Rerank 暂时不可用时可退回 RRF，返回降级状态与实际配置供评测归因。生成器取得截断的证据集合，模型返回陈述和候选引用 ID，后端检查作用域、成员资格、来源版本和原文跨度。V0.1 在无候选或无有效引用时返回 `insufficient_evidence`。V0.2 按引用和陈述分别检查支持性，同时审阅同一快照中的其他候选和问题覆盖。标注模式保留已绑定的草稿，严格模式在陈述不受支持、存在冲突或答案不完整时拒答。

**业务任务。** V0.5 的单 Agent 通过 `StateGraph` 路由 Function Calling，使用类型化参数和服务端工具注册表。工具请求经过权限检查、参数校验、时限和预算校验。知识工具返回版本化文档证据。SQL 根据受信模板和已绑定参数查询只读视图，API 参数只传给预配置的主机、路径与方法。两者保存受控结果片段及来源，报告据此绑定结论。记录任务 ID、工具执行 ID、数据快照、错误码和用量。工具超时或权限拒绝时返回清晰的任务状态，保留可核实的部分成果。V0.5 演示初值为每次 SQL 100 行和 3 秒语句超时，HTTP 请求 5 秒，每任务 8 次工具调用和 45 秒总预算。配置允许在探针、负载与实验设计明确后调整。

**持久化与恢复。** V0.6 引入 PostgreSQL 检查点。Supervisor 根据任务目标和预算调度 Knowledge Agent、Data Agent、Report Agent。Graph state 存放任务摘要和证据/执行 ID，领域记录单独保存。服务端将 `thread_id` 与应用、主体、会话所有权绑定，并串行化同一会话的写入。稳定执行键用于重放、复用已完成结果和去重。仅对预定义的瞬时故障有限重试，权限和参数错误直接返回。节点、工具和任务共同受步数、调用次数及时间预算约束。文档和工具返回值按不可信数据处理，权限决策来自服务端。

## 3. V0.1 服务契约

| 入口 | 输入 | 成功响应和关键语义 |
| --- | --- | --- |
| `POST /documents` | multipart 文件、`collection_id`、可选已有 `document_id` | `document_id`、`version_id`、`index_id`、`status=ready`、`chunk_count`。新索引 201，同输入已发布时 200 |
| `POST /questions` | JSON `question`、`collection_id`、可选有上限的 `k` | `answer_id`、`answer_status`、`answer`、`claims[]`、`citations[]`、`evidence[]`、`request_id`。无证据为业务状态 200 |
| `GET /documents/{document_id}/versions/{version_id}/units/{unit_id}` | 版本化来源地址及服务端凭据 | 返回授权原文、页码或章节位置与解析版本，支持引用重开。越权和不存在统一返回 404 |
| `GET /healthz` | 本地开发 | 响应 API 进程的存活状态 |

V0.2 保留上述接口。问答新增 `retrieval`、`verification` 和 `refusal_reason`。`GET /documents/{document_id}/versions/{version_id}/units/{unit_id}/highlight?start=…&end=…` 从授权原件复核证据坐标。只有展示端可可靠叠加原文时才返回高亮框。Markdown 或未通过几何校验的 PDF 返回文本或页码定位。

V0.1 演示用服务端配置的凭据绑定授权集合，`collection_id` 是检索目标。应用身份由入口配置决定。非法文档、空文本或无效参数返回可读客户端错误，模型或数据库失败返回依赖错误和请求 ID。输入无来源与依赖宕机使用不同状态。V0.3 评测通过离线 CLI 运行。V0.5 的 `POST /agent/tasks` 返回任务 ID、状态、报告、步骤、引用和证据，应用可在同步响应内完成有预算的演示任务。

## 4. 建议的长期目录

~~~text
EnterpriseAgent/
├── PROJECT_PLAN.md
├── README.md
├── docs/
├── pyproject.toml
├── uv.lock
├── .env.example
├── compose.yaml
├── migrations/
│   ├── core/
│   ├── course_rag/
│   └── enterprise_agent/
├── src/
│   └── enterprise_rag/
│       ├── core/
│       │   ├── documents.py
│       │   ├── embeddings.py
│       │   ├── retrieval.py
│       │   ├── generation.py
│       │   └── citations.py
│       ├── adapters/
│       │   ├── models.py
│       │   └── postgres.py
│       ├── api/
│       │   └── main.py
│       ├── evaluation/
│       └── apps/
│           ├── course_rag/
│           └── enterprise_agent/
│               ├── agents/
│               └── tools/
├── data/
│   ├── samples/
│   ├── course_rag/
│   └── enterprise_agent/
└── tests/
    ├── unit/
    ├── integration/
    └── e2e/
~~~

V0.1 只创建首轮实际使用的文档与核心模块、适配器、入口、迁移及测试。两端的 Streamlit 入口各在自己的应用包内。评测运行器和数据清单随实验阶段加入。原文、数据库与实验产物的本地运行目录配置为忽略提交，只有获得再分发授权的小样本、清单和经脱敏复核的成果可进入 Git。

## 5. 契约测试边界

- 单元测试覆盖页码和跨度、稳定切分、向量维度、生成结构、伪造引用和作用域传递。
- 集成测试用真实 PostgreSQL 执行迁移与过滤查询，模拟模型覆盖重复导入、并发发布和失败回滚。认证测试分别发起两个 scope 的读取。
- 真实模型验收在固定样例上独立运行，验证 Chat、Embedding、引用和模型用量记录。V0.5 增加真实 Function Calling 的可执行探针。
- E2E 分别从两端入口验证问答与企业报告。课程端查看来源定位，企业端核对业务快照、工具轨迹和失败状态。
