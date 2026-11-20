# V1.0 第三方依赖和样例来源

发行镜像通过锁文件安装依赖并保留 wheel 的许可证文件。基础镜像及其准确摘要见 [Dockerfile](../Dockerfile) 和 [Compose](../compose.release.yaml)，Python 直接及传递依赖见 [uv.lock](../uv.lock)。

| 组件 | 发行版依赖 | 许可证来源 |
| --- | --- | --- |
| FastAPI、Pydantic、Streamlit | FastAPI 0.165.0、Pydantic 2.14.3、Streamlit 1.67.0 | FastAPI 和 Pydantic 为 MIT，Streamlit 为 Apache-2.0。[FastAPI](https://github.com/fastapi/fastapi/blob/master/LICENSE) · [Pydantic](https://github.com/pydantic/pydantic/blob/main/LICENSE) · [Streamlit](https://github.com/streamlit/streamlit/blob/develop/LICENSE) |
| LangChain 与 LangGraph | Core 1.8.3、LangGraph 1.2.23、PostgresSaver 3.2.1 | MIT。[LangChain](https://github.com/langchain-ai/langchain) · [LangGraph](https://github.com/langchain-ai/langgraph/blob/main/LICENSE) |
| 数据库 | PostgreSQL 17、pgvector 0.8.7、psycopg[binary] 3.3.9 | PostgreSQL 与 pgvector 使用 PostgreSQL License。psycopg 采用 LGPL-3.0-only，二进制 wheel 还捆绑独立许可的库。[PostgreSQL](https://www.postgresql.org/about/licence/) · [pgvector](https://github.com/pgvector/pgvector/blob/v0.8.7/LICENSE) · [psycopg](https://github.com/psycopg/psycopg) |
| PDF 与图像 | pypdf 7.1.1、pdfplumber 0.11.10、pypdfium2 5.15.2、Pillow 13.0.0 | pypdf 为 BSD-3-Clause，pdfplumber 为 MIT，Pillow 为 MIT-CMU。PDFium wheel 附 BSD-3-Clause、Apache-2.0 及捆绑组件许可。[pypdf](https://github.com/py-pdf/pypdf) · [pdfplumber](https://github.com/jsvine/pdfplumber) · [pypdfium2](https://pypdfium2.readthedocs.io/en/stable/readme.html#licensing) · [Pillow](https://github.com/python-pillow/Pillow/blob/main/LICENSE) |
| 系统基础镜像 | Python 3.11.15 slim-bookworm 和 pgvector PostgreSQL 17 | Python、Debian 软件包及镜像所带扩展均有各自许可，二进制再分发时保留镜像和包内声明。[Python 镜像](https://hub.docker.com/_/python) · [pgvector 镜像](https://hub.docker.com/r/pgvector/pgvector) |

模型权重由使用者在 Ollama 或本地目录安装。模型与 ONNX 导出来源见 [开源调研及登记](open_source_references.md)。发行 Docker 构建只复制仓库自编企业制度、合成库存、公开课程开发集评测归档和应用配置模板。演示 PDF、Markdown 与合成销售样例可以从仓库目录读取。私有材料、备份、密钥和用户本地文件均在构建范围之外。
