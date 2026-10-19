# 智能医疗预约诊疗系统 · 后端

FastAPI 后端为患者端与医疗管理后台提供接口，使用 SQLAlchemy Async、PostgreSQL/pgvector、Redis 和 Celery；包含 DeepSeek、DashScope Embedding 及自定义 AI 工作流代码。整体架构与功能见[项目首页](../README.md)。

## 业务与 AI 模块

- **核心业务：** 预约 → 医生接诊、病历与处方 → 患者选药 → 独立问诊费/药费账单 → 窗口收费 → 药师发药。
- **身份与权限：** 患者与后台 Access/Refresh Token 分离；医生、药师、收费员、检验科、超级管理员按角色处理业务。
- **AI 分诊：** Redis 会话、RAG 分诊指引、科室查询、Prompt、LLM 生成、内容审核与科室匹配。采用代码驱动的固定工作流，不使用 LangChain `AgentExecutor`。
- **报告辅助：** 报告录入、Celery 解读任务及普通/NDJSON 流式追问；报告追问会话按患者、报告及 session_id 隔离。
- **RAG：** pgvector 向量检索 + PostgreSQL 全文检索 + RRF 融合；`rag_service.search()` 的 rerank 默认关闭，代码中保留可选能力。

这些是源码模块说明，不表示真实外部模型质量、完整 AI 诊断或检查报告业务支线已通过运行验收。

## 当前发布目录

以下路径相对于 `Backend/`，均存在于发布仓库：

```text
app/api/                       患者端、后台 API 与依赖
app/services/client/           患者业务、分诊、报告追问
app/services/backoffice/       接诊、病历、处方、窗口收费
app/services/common/           LLM、RAG、支付流水等公共服务
app/models/                    SQLAlchemy 模型
app/schemas/                   请求与响应结构
app/prompts/                   AI Prompt 模板
app/schedule/jobs/             Celery 任务
migrations/                    Alembic 数据库迁移
scripts/init_data.py           初始化四种后台角色账号
scripts/seed_drugs.py          初始化药品目录
scripts/eval_agents.py         报告解读评测脚本
tests/                         安全回归测试
.env.example                   公开配置模板，不含私有配置值
```

`init_data.py` 需要单独设置 `SEED_SUPERADMIN_*`、`SEED_PHARMACIST_*`、`SEED_CASHIER_*`、`SEED_LAB_*` 环境变量。它不创建医生档案、患者、科室、预约或报告；药品初始化也不能代替业务数据准备。

`eval_agents.py` 包含合成报告、规则检查和 LLM-as-judge 逻辑，会写数据库并调用模型。脚本存在不等于评测已通过；本轮验收未执行它，不据此发布 AI 准确率或临床效果指标。

## 开发运行参考

可按[首页运行说明](../README.md#本地运行参考)准备独立环境：

1. 使用 Python 3.12 和 `requirements.txt`；依赖声明包含 `passlib==1.7.4`、`bcrypt==4.0.1`。不要复用生产数据库或患者数据。
2. 参照 `.env.example` 准备私有配置，填写数据库、Redis、非空 JWT `SECRET_KEY` 等参数；缺失或全空白 JWT 密钥会在配置加载时失败。
3. Compose 开发参考使用 `docker-compose.yml` 与 `docker-compose.dev.yml`；设置独立项目名、容器名相关的 `PROJECT_NAME`、`RUNTIME_DIR` 和端口。仅更换 Compose 项目名不足以避免固定容器名冲突。
4. 数据库目标确认后才执行 Alembic 迁移、账号或药品初始化。迁移与初始化会写数据；本文没有提供对现有环境自动执行的脚本。
5. 健康路径为 `/api/v1/config/health`，默认 API 端口 8001。`main.py` 开发模式会启用热重载；独立验收使用单进程专用启动包装器，不能混为同一部署方式。

S3 客户端在模块导入时初始化；AI、邮件、Celery 等需要独立配置。仅填一个 JWT 密钥或看到健康检查成功，不能证明外部调用与异步任务可用。没有外部调用需求时，应先明确替身和网络隔离方案。

## 验收结果与边界

截至 2026-10-18，A 阶段完成独立 Docker/FastAPI/PostgreSQL/Redis 基础健康验证；B、C 针对提交 `29d505f7d6cdcc966fe787fb7638d5aa9772150f` 完成 101 项安全集成检查、46 项后端业务检查及 12 项浏览器冒烟检查。详见[首页验收结果](../README.md#独立环境验收结果)。

仓库内 `tests/` 包含 AWS 入口、JWT 配置、Refresh Token、报告追问回归测试，部分使用隔离替身。159 项是仓库外独立集成验收的分组汇总，不等于 `tests/` 中有 159 个测试，也不意味着整体覆盖率为 100%。

未验证范围包括真实 LLM/RAG 质量、完整 AI 医疗诊断、线上支付回调、等待检查报告支线和并发刷新/退出。没有把旧环境历史结果或外部服务替身通过描述为真实提供方验收；独立派生镜像验证也不等于原 Dockerfile 从零构建成功。

本后端用于技术演示，不适用于真实医疗机构生产环境。项目使用范围见[项目使用说明](../README.md#项目使用说明)，第三方依赖遵循各自的许可证。
