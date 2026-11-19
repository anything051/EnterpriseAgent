# 开源组件与引用管理

> V0.0 候选清单。**这里不宣称已 Fork、已复用、已验证许可证或已做依赖兼容性测试**。这些核查应在相应代码进入仓库前执行并记录。

## 1. 候选技术与仓库

| 项目 | 官方仓库 | 拟用方向 | 当前行动 |
| --- | --- | --- | --- |
| LangChain | https://github.com/langchain-ai/langchain | 文档处理、RAG 组件 | 验证当前稳定 API 与依赖 |
| LangGraph | https://github.com/langchain-ai/langgraph | Agent 状态、工具工作流、持久化 | 验证版本、工具调用与检查点 |
| pgvector | https://github.com/pgvector/pgvector | PostgreSQL 相似度检索 | 验证数据库/扩展版本 |
| Ragas | https://github.com/vibrantlabsai/ragas | 检索、回答和 Agent 评测 | 核对指标 API 与限制 |
| MaxKB | https://github.com/1Panel-dev/MaxKB | 企业知识库与应用设计参考 | 评估是否只参考思路，核查许可证 |
| Dify | https://github.com/langgenius/dify | 工作流与知识库交互设计参考 | 核查许可限制和组件复用范围 |
| LightRAG | https://github.com/HKUDS/LightRAG | 可选知识图谱检索方向 | 当前不进入 MVP |

优先组合轻量级 LangChain/pgvector/LangGraph 自建应用层；**不直接合并 MaxKB 与 Dify 两套大平台**。对项目源码的复制、分发或修改，必须逐个核实 LICENSE、NOTICE、子目录许可与版本；如果许可条件无法满足，就只参考公开设计思想，不复制代码。

## 2. 代码来源登记模板

当首次复制/改编外部代码时，登记：

- 上游仓库 URL、文件路径、版本/commit SHA、访问日期
- 源文件许可证和版权声明
- 本项目使用位置
- 是否直接复制、修改、重新实现、仅参考
- 修改点、保留的署名/NOTICE、是否需要开源衍生代码

登记资料不得凭印象填写。依赖库按包名与锁定版本记录。

## 3. 学术参考方向（阅读与实际引用分开）

- ALCE / 引用生成与评测：https://aclanthology.org/2023.emnlp-main.398/
- RAG attribution correctness vs faithfulness：https://research.tudelft.nl/en/publications/correctness-is-not-faithfulness-in-retrieval-augmented-generation/
- 课程场景相关工作需继续系统检索；正式毕设论文中必须核对论文作者、发表年份、研究结论与引用格式。

**创新表述边界**：混合检索、Rerank、细粒度引用、证据不足拒答均已有相关研究；本项目拟做的贡献是课程场景下的工程化实现、组合优化和可复现实验，不能宣传为从未提出的原创算法。

## 4. V0.1 前检查

1. 核对 Python/平台环境及稳定依赖版本。
2. 核对模型 API 是否支持需要的功能；Embedding 与 LLM 可分离。
3. 核对计划使用的开源实现的许可证和引用。
4. 选定最小集成范围，确认没有隐式拉入复杂框架。
5. 将结论写入本文件，标注实际检查日期与版本。
