# 数据结构与证据模型（设计稿）

> V0.0 只规定逻辑契约；数据库 DDL、字段类型、索引、迁移在 V0.1/V0.2 实现时确定。

## 1. 数据实体

| 实体 | 建议必要字段 | 设计要点 |
| --- | --- | --- |
| ApplicationScope | app_id、tenant_id(可选)、permissions | CourseRAG 与 EnterpriseAgent 默认隔离 |
| Document | document_id、scope_id、file_hash、version、filename、mime_type、source_uri、ingest_time、parser_version | 可重新解析和增量导入；原始文件存储位置可审计 |
| Section | section_id、document_id、course_id、chapter_path、title | CourseRAG 的章节组织；不强加于企业数据 |
| Chunk | chunk_id、document_id、version、page_start/end、section_id、char_start/end、text、metadata | 与原文切片有稳定映射；跨页分块要记录多段来源 |
| Embedding | chunk_id、model_id、model_version、dimension、vector、created_at | 模型改变需重建相关索引 |
| Evidence | evidence_id、source_type、source_id、scope_id、text_or_data、locator、retrieval_run_id、score | 统一外部对象，但不同类型分别做验证 |
| Citation | citation_id、answer_id、claim_span、evidence_id、support_status、validator_version | 由后端绑定实际 Evidence，不能凭模型生成虚构引用 |
| EvaluationSample | sample_id、split、question、gold_answer、gold_evidence、answerability、tags | 记录人工标注依据 |
| EvaluationRun | run_id、model_id、config_hash、data_version、seed、timestamp、sample_results | 能复现结果及统计口径 |
| ToolExecution | execution_id、tool_id、request_params、status、started_at、finished_at、source_refs、error_code | 敏感参数与凭据须脱敏 |

## 2. 统一 Evidence 示例

~~~json
{
  "evidence_id": "ev_doc_0001",
  "source_type": "document",
  "scope_id": "course_demo",
  "source_id": "doc_os_edition1",
  "content": "页表保存虚拟页到物理页框的映射。",
  "locator": {
    "document_version": "v1",
    "page": 12,
    "chunk_id": "chunk_0012",
    "char_start": 110,
    "char_end": 129,
    "pdf_bboxes": []
  },
  "retrieval": {
    "run_id": "ret_001",
    "rank": 1,
    "score": null
  }
}
~~~

此为**接口示例**而非实际输出。对 PDF：位置可取页码、字符跨度、段落、坐标；没有可靠坐标时 pdf_bboxes 为空且 UI 退化到页码+原文片段。

## 3. 业务 SQL 来源记录

~~~json
{
  "evidence_id": "ev_sql_0001",
  "source_type": "sql_result",
  "scope_id": "enterprise_demo",
  "source_id": "query_execution_001",
  "content": {
    "product_id": "p100",
    "sales_count": 42
  },
  "locator": {
    "query_template_id": "top_product_sales",
    "parameter_hash": "recorded-at-runtime",
    "dataset_version": "synthetic-v1",
    "result_row_key": "p100",
    "executed_at": "runtime-value"
  }
}
~~~

原则：SQL Evidence 对应真实的执行结果、数据版本、允许查看的查询记录；不把 SQL 聚合结果伪装成 PDF 原文证据，也不能仅凭该结果推断业务因果关系。

## 4. Citation 与支持性校验

- 生成模型输出的引用仅是**候选引用请求**，必须检查 evidence_id 是否属于此次已授权检索/工具执行集合。
- 将答案中的可验证陈述拆为 claim，再对“证据是否支持该 claim”进行标注/自动判定（supported、unsupported、contradicted、uncertain）。
- 无证据或证据冲突需向用户显式表达；校验模型自身也可能出错，因此保留人工样本评估。
- 文档引用准确 ≠ 模型推理过程忠实；指标中单独区分。
- 报告可同时引用文档、SQL 和 API 证据，但各自对应不同验证器。

## 5. 数据隔离、版本与留存

- 每次检索附带 scope_id，服务端强制过滤，不能只依赖前端。
- 为课程资料保留课程、章节、学期、文档版本；不同版本冲突时明确提示。
- 生成测试数据时固定随机种子和脚本，避免把真实敏感数据提交仓库。
- 不将真实 API Key、数据库口令、个人隐私或完整敏感工具参数写入日志。
- 可采用数据删除/重建策略；文档撤销后应删除对应 Embedding 和缓存引用。
