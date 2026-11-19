# EnterpriseRAG 数据与证据设计

本文件定义实体、键、位置语义和生命周期。V0.1 先实现文档来源与索引，后续根据路线图扩展课程、业务和评测实体。

## 1. 共享核心实体与关系

| 实体 | 主键及主要字段 | 关系与约束 |
| --- | --- | --- |
| Scope | `scope_id`、`app_id`、`collection_id`、可选 `tenant_id` | 一个作用域拥有多个文档。服务端依据主体和应用授权解析 scope |
| Document | `document_id`、`scope_id`、展示名、来源登记 | 代表逻辑资料。一份资料可有多个不可变版本 |
| DocumentVersion | `version_id`、`document_id`、`revision`、原始文件 SHA-256、MIME、原件存储引用、采集时间、许可 | 同一文档内的修订号和内容哈希各自唯一。路径、名称和上传时间均可变化 |
| SourceUnit | `unit_id`、`version_id`、`parser_profile_id`、页/段定位、`source_text`、文本哈希 | 单一解析快照中的原文坐标系。PDF 每页一个单元，Markdown 每个可定位段落一个单元 |
| IndexBuild | `index_id`、`version_id`、`index_profile_id`、状态、创建/发布时间、失败码 | 同一版本和配置指纹对应一个构建记录。一个构建接入多个 Chunk |
| Chunk | `chunk_id`、`index_id`、`unit_id`、`paragraph_id`、`start`、`end`、内容哈希 | Chunk 在同一单元、同一段落内切分。ID 由构建身份、单元及范围稳定导出 |
| Embedding | `chunk_id`、`embedding_profile_id`、`dimension`、`vector` | 索引表按已配置维度保存。Query 和文档向量必须来自同一 Embedding 配置 |
| Answer / Claim | `answer_id`、请求/任务 ID、状态、`claim_id`、陈述、答案中的可选跨度 | 一个答案包含多个陈述。最终呈现可由结构化陈述渲染 |
| Evidence / Citation | `evidence_id`、来源类型、作用域、快照定位和 `citation_id`、`answer_id`、`claim_id`、`evidence_id` | 一个陈述可引用多个 Evidence，一个 Evidence 可被多个陈述引用。每条边保留绑定和校验信息 |

关系主链为 Scope → Document → DocumentVersion → SourceUnit，以及 DocumentVersion → IndexBuild → Chunk → Embedding。Chunk 引用 SourceUnit，Citation 连接 Claim 和 Evidence。索引构建、原文和证据读路径均通过所属 scope，外键或复合约束避免将来源关联到其它作用域。

## 2. 原文定位与索引生命周期

- PDF 页码从 1 开始，单独记录可选的印刷页标签。段落以固定版本的提取规则划分并保存边界。Markdown 保留 UTF-8 原文件、标题路径、源文件行区间和段序。所有字符范围用零基 Unicode 码点半开区间 `[start, end)`，相对于对应不可变的 `source_text`。展示片段必须等于该字符串切片。清洗用于 Embedding 的文本时单独派生内容。
- PDF 高亮候选在 V0.2 使用可验证的 `bboxes`。记录坐标系、点单位、页面盒、旋转及提取器版本。使用带文字夹具对照渲染验证。坐标不可用时仍可按页码、段落和文本打开原文。读屏和网页若用 UTF-16 偏移，在渲染端映射。
- 解析配置指纹涵盖解析器、版本和文本规范。索引配置指纹另含切分参数、Embedding 供应商/模型标识/版本或配置指纹、向量维度和距离类型。逐次重建保留记录。同一配置下切分位置和 Chunk ID 可重现。
- 新导入按 scope、逻辑文件键与文件哈希复用版本，显式提供已有 document ID 可提交新修订。文件哈希以原始字节计算。上传存储使用按哈希命名的不可变对象，失败可留下待清理的未引用对象。相同构建的并发提交由唯一键和事务协调。
- 入库在短事务外完成解析和向量生成。成功后在事务内保存索引并发布，查询只读 `ready` 构建。中途失败保持旧的发布指针。采用修订号与发布条件避免旧任务覆盖较新的发布。失败重试不产生重复 Chunk 或 Embedding。
- V0.1 每个 collection 使用一个激活索引配置。每个文档可有多次构建，检索只读取该配置下发布的文档版本。切换向量模型或维度时先建匹配维度的索引空间，完成全量构建后切换 collection 的激活配置。查询检索和证据绑定始终使用同一请求快照。

## 3. Evidence 和 Citation

Evidence 通用信封包含 `evidence_id`、`scope_id`、`source_type`、`source_id`、`locator`、`content`、`content_hash`。类型化 `locator` 随来源变化。来源身份来自授权快照和位置。候选排序、得分、检索阶段与运行 ID 另记在检索轨迹中，使同一证据可跨请求重用。以下为接口示意。

~~~json
{
  "evidence_id": "demo-evidence-1",
  "scope_id": "course-demo",
  "source_type": "document",
  "source_id": "demo-document",
  "content": "页表保存映射。",
  "locator": {
    "version_id": "demo-version-1",
    "parser_profile_id": "demo-parser-1",
    "unit_id": "page-12-profile-1",
    "page": 12,
    "paragraph_id": "p3",
    "start": 0,
    "end": 7,
    "chunk_id": "demo-chunk-1",
    "bboxes": []
  }
}
~~~

其中 `content_hash` 由持久化层根据实际内容计算。文档的来源读路径使用 `document_id`、`version_id`、`unit_id`，用户可在索引切换后复核此前回答。API DTO 可按需要裁剪取证字段。

| 来源 | 定位与保存 | 校验 |
| --- | --- | --- |
| 文档 | 版本、解析配置、单元、段落、跨度、Chunk 和可选坐标 | 回读不可变原文并逐字比较。V0.2 判断这组引文对陈述提供支持、冲突或信息不足 |
| SQL | 执行 ID、模板 ID 和版本、允许保留的参数或哈希、业务快照、结果行键/列、时间、结果摘要哈希 | 验证该执行真实成功且授权，按结果列、单位、过滤条件和快照核对数字及可复算聚合 |
| API | 执行 ID、端点 ID、响应字段路径、时间、上游版本/快照、响应摘要哈希 | 核对授权、响应状态、结构、时间和保留的字段投影。字段缺失、快照过期或无法复核时标记不确定 |

模型可提出候选证据 ID。后端只为本请求已授权的 Evidence 生成 Citation。Citation 记录 `binding_status`（已绑定）、`support_status`（`unchecked`、`supported`、`unsupported`、`contradicted`、`uncertain`）、验证器版本、依据和时间。V0.1 的有效引用处于 `unchecked`。V0.2 校验单条证据和同一陈述的证据组合，缺引用、组合支持和冲突均留痕。业务推断应标明假设，数值和事实可分别回核。

## 4. 课程、企业和评测数据

| 数据域 | 模型与关系 |
| --- | --- |
| CourseRAG | `Course` 属于课程 scope，`Chapter` 有父子关系和顺序，`KnowledgePoint` 归章节并可关联多段材料。`CourseMaterial` 将章节和不可变文档版本相连。记录学期、课程授权和课程当前发布资料 |
| EnterpriseAgent | `Product` 关联 `OrderLine`、`InventorySnapshot`，`Order` 关联订单明细。保存业务时间区间、时区、数量与币种。制度文档仍走核心，文档 scope 和业务数据角色由应用授权 |
| Agent 执行 | `BusinessDatasetVersion` 记录快照和种子。`TaskRun` 拥有 `ToolExecution`，记录工具/模板版本、归属、状态、时间、错误码、结果投影/哈希。SQL/API Evidence 引用成功执行。会话表关联私有线程及检查点 |
| 实验 | `EvaluationSample` 按应用和数据版本保存问题、split、可回答性、gold answer、gold evidence/结果、标签。 `EvaluationRun` 关联配置、提交和数据快照，并拥有逐例评分与轨迹。字段细则见 [评测协议](evaluation.md) |

## 5. 隔离与保留

- 两应用分别配置数据库连接、运行身份、评测数据和原件存储路径。开发与生产分开。课程集合和企业知识库还使用服务端 scope 过滤，覆盖列表、检索、原文访问、会话与缓存命中。缓存键含作用域和发布配置。
- schema 迁移使用管理凭据，核心运行账号只拥有实际所需写权限。企业查询工具另用专用只读账号和受限视图，固定表/模板、绑定参数、事务只读、数据库级超时与结果上限。
- 版本归档保留授权原文与必要的来源指针。撤销访问后立刻停止检索和读取，清理向量、BM25 缓存和可展示快照，并在历史引用处显示不可用。最终保留策略按资料许可和环境配置执行。
- 日志保留请求/执行 ID、模型用量、耗时、判定、错误码和必要的来源引用。结果快照按权限最小化字段并限制留存期限。环境变量中的密钥与认证头、完整敏感参数隔离于记录之外。
