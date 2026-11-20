# CourseRAG 本地演示

CourseRAG 有独立的 FastAPI 和 Streamlit 入口。它用课程目录组织章节、知识点和材料，由 Trusted RAG Core 处理材料索引、检索、证据审阅和拒答。演示使用一个本地管理员令牌，适用于本机操作。

## 准备

1. 参照 [主项目启动说明](../README.md#本地启动) 配置 `.env`、PostgreSQL、Chat 和 Embedding。运行 `docker compose up -d --wait`。V0.4 可沿用已有数据库实例。需要物理隔离时，将 `COURSE_RAG_DATABASE_URL` 指向另一个已经迁移的实例。
2. 在 `.env` 设置 `COURSE_RAG_TOKEN`，用 `openssl rand -hex 24` 生成独立随机值。使用默认端口时可省略 `COURSE_RAG_API_URL`。课程应用默认为 `COURSE_RAG_APP_ID=course-rag-v04`，每门课的检索范围由服务端根据其 ID 生成。
3. 已有 Compose 数据卷须手动应用缺少的迁移。空数据卷启动时自动执行。已有 V0.1 卷还需先执行 README 所列的 `003_highlight_source.sql`。

~~~bash
docker compose exec -T db psql -v ON_ERROR_STOP=1 -U rag_owner -d enterprise_rag < migrations/course_rag/004_catalog.sql
~~~

## 启动

在仓库根目录开两个终端。若 `.env` 中配置了 BGE 模型目录，先按 README 核验模型文件，并在两条 `uv run` 命令中加 `--extra rerank`，保持两端使用同一个依赖环境。

~~~bash
uv run --locked --extra course uvicorn enterprise_rag.apps.course_rag.api:create_course_app --factory --host 127.0.0.1 --port 8001
~~~

~~~bash
uv run --locked --extra course streamlit run src/enterprise_rag/apps/course_rag/ui.py --server.address 127.0.0.1 --server.port 8501
~~~

打开 [课程工作台](http://127.0.0.1:8501)，在界面输入课程令牌。服务端从 `.env` 读取令牌，授权当前应用的所有课程。`http://127.0.0.1:8001/docs` 提供接口调试。API 和界面均应绑定回环地址。容器方式见 [统一部署指南](deployment.md)。

## 演示流程

1. 新建「操作系统」。在「课程资料」添加「内存管理」章和「页面置换」知识点。登记 PDF 材料，上传 [操作系统笔记](../data/samples/course_memory.pdf)，确认版本列表显示当前版本。重复上传相同字节会复用已发布版本。
2. 在「问答」输入「在 LRU 页面置换算法中，优先淘汰哪个页面？」。核对答案为最长时间未被访问的页面。选择对应陈述的引用，看 PDF 第 2 页高亮与原文切片。在模型结构化输出异常时，界面会提示重试。模型支持性判断存在波动，可据原文复核。
3. 新建「计算机网络」，添加章节并发布 [链路层实验导读](../data/course_rag/demo/network.md)。在这门课询问练习链路的 IP MTU，预期证据为 1500 字节及 Markdown 行号。再新建一个没有材料的课程，验证相同问题返回证据不足。切换回操作系统，仍可打开原来的引用。
4. 打开「评测」并下载逐例记录与报告。默认展示已经运行过的 [V0.3 A–D 开发集](evaluation_runs/course-dev-20261120/report.md)。报告包含错误案例和拒答结果，可按运行配置离线复算。

新完成的评测默认写入 `runs/`。人工审阅待展示内容和授权后，为该运行生成文件清单。将下面的 `my-run-id` 替换为实际运行 ID，并在 CourseRAG API 的环境中设置 `COURSE_RAG_EVALUATION_ROOT=runs` 后重启。重新复算或补充 Ragas 文件后可重新生成清单。

~~~bash
uv run --locked python -m enterprise_rag.evaluation archive runs/my-run-id
~~~

仅支持可提取文本的 PDF 和 UTF-8 Markdown。对于旋转、裁切或无法可靠匹配字形的页面，界面展示整页和原文切片。模型与 Embedding 的配置改变可能需要新建课程，再发布材料以生成兼容索引。当前目录操作覆盖新增、浏览与版本发布。
