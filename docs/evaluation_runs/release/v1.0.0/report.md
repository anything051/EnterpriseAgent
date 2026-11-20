# V1.0 双应用发布验收

2026-11-21 在 macOS Docker Desktop 的 Linux arm64 容器运行。构建来源为 `66e085b0d67973c92a6d1dd718595774f4164ebc` 的已提交源码。API 调用本机 Ollama 0.23.1、Qwen 3.5 9B 和 nomic-embed-text。检索为混合 RRF，材料和业务数据来自仓库演示样本。

| 验收 | 结果 | 证据 |
| --- | --- | --- |
| 全新卷与两套 API／UI | 11/11 | [真实执行](live.json) |
| 课程原文与引用 | 中文 PDF 回到第 2 页并生成高亮，Markdown 回到第 7 行，空课返回证据不足 | [真实执行](live.json) |
| 企业报告 | 固定销售、库存和制度证据均通过复核，历史报告重复读取一致，查阅角色不能取得业务结果 | [真实执行](live.json) |
| 运行隔离与日志 | 两库 schema 隔离，企业读账号无业务写权限，5 个应用容器非 root 且只读，令牌与请求测试标记未出现在日志 | [运维记录](ops.json) |
| 双库恢复 | 6/6，恢复版的 PDF 高亮与原实例一致，重开 Markdown 及企业报告通过，并使用恢复索引重新问答 | [恢复实测](restored.json) · [备份清单](snapshot_manifest.json) |
| 回切与防覆盖 | 原实例的来源与报告保持可读，脚本拒绝还原到原项目和已有目标库，界面验证角色边界 | [回切记录](switchback.json) |

验收对功能和恢复行为逐项判分。90 项常规测试通过。V0.3 [课程 A–D](../../course-dev-20261120/report.md)、V0.5 [企业 E0/E1](../../enterprise/enterprise-dev-20261120/report.md) 和 V0.6 [企业 E1/E2](../../enterprise/enterprise-reliability-20261120/report.md) 均保留逐例数据，当前代码已验证其离线复算。V1.0 结果用于固定演示用例和恢复验证。

[部署和复现命令](../../../deployment.md) · [阶段交接](../../../handoff/V1.0.md)
