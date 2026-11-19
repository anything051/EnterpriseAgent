# 系统架构与接口边界

> 状态：V0.0 设计草案，非已实现架构。

## 1. 分层架构

~~~mermaid
flowchart TD
    UI1[CourseRAG UI] --> APP1[CourseRAG Application]
    UI2[EnterpriseAgent UI] --> APP2[EnterpriseAgent Application]
    APP1 --> CORE[Trusted RAG Core]
    APP2 --> CORE
    APP2 --> AGENT[LangGraph Agent Orchestrator]
    AGENT --> KTOOL[Knowledge Retrieval Tool]
    AGENT --> SQL[Read-only SQL Tool]
    AGENT --> API[Allowlisted Business API Tool]
    KTOOL --> CORE
    CORE --> PARSE[Parser and Chunk Index]
    CORE --> RETRIEVE[Vector / BM25 / Rerank]
    CORE --> VERIFY[Evidence and Citation Validator]
    PARSE --> STORE[(PostgreSQL + pgvector)]
    RETRIEVE --> STORE
    APP1 --> EVAL[Evaluation and Audit]
    APP2 --> EVAL
    CORE --> EVAL
~~~

**共享部分仅包含业务无关功能。** CourseRAG 对课程章节负责；EnterpriseAgent 对业务授权、工具执行、任务编排负责；共用代码不意味着共用数据。

## 2. 共享模块职责

| 模块 | 输入 | 输出 | 约束 |
| --- | --- | --- | --- |
| document | 文档二进制、元数据、租户/应用域 | 带页码/偏移/位置的 Document 与 Chunk | 不丢原始文件关联，扫描 PDF 可标注低可信 |
| embedding | Chunk 文本、模型版本 | 向量、维度、版本 | 模型变更后需重建向量 |
| retrieval | query、scope、过滤条件、K | Evidence 候选与分数 | 不允许跨应用/课程越权检索 |
| generation | question、Evidence IDs、上下文 | 答案草稿、声明性 citation IDs | 引用 ID 必须来自候选证据 |
| citation | 答案、证据与原始定位 | 已验证的 Citation/定位信息 | 不由 LLM 自行决定真实来源 |
| evaluation | 测试样例、流水线版本、执行轨迹 | 样本级分数和聚合结果 | 真实运行，保留配置 |

## 3. 数据流

### 3.1 文档入库

1. 上传或导入公开/许可文档，分配 Document ID 与版本。
2. 解析 PDF/Markdown，保留页码、段落、文本跨度；可获得时保留 PDF 坐标。
3. 分块，建立 Chunk 到原始文档区间的可追溯映射。
4. 使用固定版本的 Embedding 生成向量并索引。
5. 写入权限范围与索引版本，确保重复运行可判定增量/重建。

### 3.2 问答与证据引用

1. 校验用户和应用的知识库 scope。
2. 检索候选块，V0.2 增加 BM25、融合和 Rerank。
3. 生成受限于候选 Evidence ID 的答案草稿。
4. 校验引用是否属于本次候选结果，映射回 document/page/span。
5. V0.2 以后针对事实性结论作证据支持性判断，不支持则修改、标记或拒答。
6. 将结果、Evidence、模型与检索配置写入必要的审计轨迹。

### 3.3 EnterpriseAgent 业务任务

1. LangGraph 根据任务选择 Knowledge Tool / SQL Tool / API Tool。
2. 每种工具返回统一 Result + Provenance 结构（不能只返回自由文本）。
3. 业务报告只引用实际工具返回的数据；报告级证据校验独立实现。
4. 工具失败时有受控重试、可见错误，不得伪造结果。

## 4. 最小接口约定（目标，不是已存在的 API）

~~~text
POST /documents             -> document_id, version, parse_status
POST /questions              -> answer, citations[], evidence[], uncertainty
GET  /documents/{id}/pages/{page} -> page content / authorized viewer data
POST /evaluations/run        -> run_id, metrics (V0.3)
POST /agent/tasks            -> task_id, result, steps, sources (V0.5)
~~~

具体 HTTP 路由与实体字段以 V0.1 实现时的 OpenAPI 为准。

## 5. 数据契约（高层）

- **Document**：文档 ID、版本、应用/课程或租户范围、文件哈希、媒体类型、权限。
- **Chunk**：Chunk ID、Document ID、页码或章节、原文片段、原文跨度、解析器版本。
- **Evidence**：Evidence ID、source_type、source_id、content、locator、索引版本、检索分数。
- **Citation**：answer claim/span、Evidence ID、support verdict、可跳转定位。
- **ToolResult**：tool_name、execution_id、data、source records、executed_at、status、error。

Schema 详细要求见 [data_design.md](data_design.md)。

## 6. 技术难点与回退策略

- PDF 图文/公式混排造成坐标映射偏移：先确保文档 ID + 页码 + 短片段可靠，坐标高亮作为增强。
- BM25 与向量分数不可直接相加：考虑 RRF 等融合方法，记录 K 和权重。
- 引用真实性：候选 evidence ID 必须来自实际检索；即使有引用仍需验证支持关系。
- 来源冲突：记录文档版本/时间和冲突提示，不自动宣称某证据权威。
- Agent 的 SQL 安全：使用只读凭据、受控查询模板、参数化查询和执行限制；不执行未经审批的自由 SQL。

## 7. 接口测试边界

- 核心模块有纯函数/数据库集成测试。
- CourseRAG 测试章节过滤与引用跳转。
- EnterpriseAgent 测试工具参数、安全拒绝、执行顺序、来源保持。
- 端到端测试验证从 PDF 导入到答案引用以及从业务问题到带来源报告。
