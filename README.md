# EnterpriseAgent / EnterpriseRAG

> 项目当前状态：**V0.0 方案规划完成，业务功能尚未实现**。仓库名称保留为 EnterpriseAgent；总体架构称为 EnterpriseRAG。

本仓库计划建设一套**可复用的 Trusted RAG Core（可信检索增强生成核心）**，支撑两个场景：

- **CourseRAG（本科毕设）**：《基于 RAG 的课程知识智能问答与证据溯源系统设计与实现》。重点是课程检索、细粒度证据定位、引用支持性校验和对照实验。
- **EnterpriseAgent（2027 届春招展示项目）**：企业知识库 + LangGraph + Function Calling + 安全 SQL/API 工具 + 多智能体业务分析，复用 Trusted RAG Core 实现可信引用。

核心原则：**共用技术底座，隔离业务应用、数据、配置与评测；使用开源组件但明确贡献；先验证 RAG，再扩展 Agent。**

## 规划文档

| 文档 | 内容 |
| --- | --- |
| [PROJECT_PLAN.md](PROJECT_PLAN.md) | 目标、边界、需求、技术栈、交付物 |
| [docs/architecture.md](docs/architecture.md) | 模块架构、数据流、接口边界 |
| [docs/roadmap.md](docs/roadmap.md) | V0.0–V1.0 开发阶段和验收门槛 |
| [docs/data_design.md](docs/data_design.md) | Document / Chunk / Evidence / Citation 数据模型 |
| [docs/evaluation.md](docs/evaluation.md) | 基线实验、指标与复现规范 |
| [docs/open_source_references.md](docs/open_source_references.md) | 开源复用、许可证核查与来源登记 |
| [docs/CODEX_V0_1_PROMPT.md](docs/CODEX_V0_1_PROMPT.md) | 第一轮编码（V0.1）给 Codex 的执行指令 |

## 计划采用的技术

Python 3.11、FastAPI、Pydantic、LangChain、LangGraph、PostgreSQL + pgvector、BM25、Reranker、Ragas、pytest、Streamlit、Docker Compose。具体依赖版本、许可证与模型能力应在编码前以官方文档和环境验证为准。

## 当前行动

审核 PROJECT_PLAN.md 与文档设计，明确最小数据集和模型供应商配置；然后按照 [V0.1 开发指令](docs/CODEX_V0_1_PROMPT.md) 实现最小可运行 RAG。**当前仓库不得被描述为已经交付的系统或完成的实验。**
