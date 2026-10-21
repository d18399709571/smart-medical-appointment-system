# 本地最小演示与本轮验证边界

此流程只用于合成数据演示。必须使用干净的独立克隆、全新的 Compose 项目名和数据库卷；不要在旧数据库上运行迁移或初始化脚本。

## 先决条件

- Python 3.12（配置生成脚本只使用标准库）、Docker Engine、Docker Compose V2。
- 后端构建需要联网下载 Dockerfile.dev 中声明的系统包和 Python 依赖；本轮实际验收复用已有依赖镜像，**未验证全新依赖下载或镜像从零构建**。
- 约 2 GiB 可用运行内存，以及镜像和数据库所需的额外磁盘空间。不要通过清理旧卷腾空间。
- 以下命令在仓库根目录使用 PowerShell 执行。`D:\MedicalDemoV2` 是本次新建的配置目录，不是旧项目运行目录。若已存在配置，生成器会拒绝覆盖。

## 配置、启动、迁移、合成账号

```powershell
python -B Backend/scripts/demo_env.py D:\MedicalDemoV2\demo.env
$demo = @('--env-file', 'D:\MedicalDemoV2\demo.env', '-p', 'medical-demo-v2-new', '-f', 'Backend/compose.demo.yml')
docker compose @demo build api
docker compose @demo up -d --no-build --wait --wait-timeout 90
docker compose @demo exec -T api alembic upgrade head
docker compose @demo exec -T api python -B -m scripts.seed_demo
Invoke-RestMethod http://127.0.0.1:18091/api/v1/config/health
```

每条命令成功后再执行下一条。生成器会准备只读源码挂载所需的空 `Backend/logs` 目录。日志写入容器内 tmpfs，停止容器前应导出需要保留的合成测试日志。

已有兼容依赖镜像时，可在**新生成**的 `demo.env` 增加 `DEMO_IMAGE=你自己的镜像标签`，跳过 `build`，使用 `up -d --no-build --pull never --wait --wait-timeout 90`。这只复用依赖；Compose 将当前克隆的 `Backend` 只读挂载到 `/app`，并显式启动当前源码中的 `main:app`，不使用旧镜像的入口包装器。必须核对实际挂载来源，不能用镜像标签推断源码版本。

配置文件中的 `DEMO_SECRET_KEY`、`DEMO_DB_PASSWORD`、`DEMO_LOGIN_PASSWORD` 都是随机生成的临时值。登录密码只在本地配置文件查看，勿复制到日志、报告或仓库。账号：

| 角色 | 合成邮箱 |
| --- | --- |
| 患者 | patient@example.com |
| 医生 | doctor@example.com |
| 药师 | pharmacist@example.com |
| 收费员 | cashier@example.com |

初始化脚本仅接受 `postgres/medical_demo` 目标，遇到非空账号/科室表即拒绝。合成账号没有真实患者资料。可通过患者注册/登录、医生/科室查询、预约、后台接诊、处方、窗口结算和发药接口准备演示链路。后台没有生成超级管理员账号。

Nginx 入口仅绑定 `127.0.0.1:18091`，API、PostgreSQL/Redis 不发布宿主端口，位于 internal 网络，无外联路由。Nginx 同时连接专属入口网络，将本机请求转发到 API；这是为兼容 Docker Desktop 对 internal 网络端口发布的限制。所有卷和网络属于新项目。当前入口的 `/` 文档导航在 `ENV=testing` 下关闭；接口说明可查源码中的 API/Schema。健康检查只证明健康检查的范围。

## 可复现的数据库并发检查

仅在上述专用数据库中执行；脚本会新增合成记录，不清空数据：

```powershell
docker compose @demo exec -T -e ALLOW_DISPOSABLE_CHECKS=yes -e CHECK_LABEL=local-fixed api python -B tests/check_business_concurrency.py
docker compose @demo exec -T -e ALLOW_DISPOSABLE_CHECKS=yes api python -B tests/check_demo_http.py
docker compose @demo exec -T api python -B -m unittest discover -s tests -v
```

第一条使用真实 PostgreSQL 独立连接与事务，通过异步暂停点控制竞争，并记录 `pg_blocking_pids` 或 B 在 A 暂停期间完成的证据。JSON 行包含 Service 返回结果、业务错误对应 HTTP 状态、最终真实记录与汇总。它不是 HTTP 并发压测。第二条执行合成账号的真实 HTTP 业务链；第三条为原有安全回归，部分使用隔离替身，不能作为真实并发数据库证据。

预约规则：同医生、同一精确时间最多一条非 `cancelled` 预约；取消释放时间；不同医生不冲突。不提供预约时长或号源设计。新迁移 `c4e82a19bd70` 会拒绝已有重复活跃时刻，不自动删改重复记录；不要在本流程以外的数据库直接执行。

## 外部能力与停止

- 上述核心业务与窗口记账无需 LLM、S3 或邮件 API Key。现金/刷卡是演示记账，不代表真实到账。
- 线上支付接口会向 Redis 投递模拟回调任务。本最小环境不启动 Celery Worker，因此不会自动完成该任务；并发脚本直接执行真实回调 Service，不能据此声称 Celery Worker 链路已验收。
- 报告解读、智能分诊/RAG、S3 文件上传、邮件发送需要自行配置相应外部服务，当前临时占位值无效且网络禁止外联。该环境没有外部服务替身，也未验证真实外部能力。
- 不启动 Beat、Flower或前端。本轮验证 API 最小演示；前端开发命令和代理参数见各前端 README，本轮未重跑浏览器。

安全停止，保留本次数据卷：

```powershell
docker compose @demo stop
```

确认项目名后也可使用 `docker compose @demo down` 移除本项目容器和网络；不要添加 `-v`，不要执行全局清理。

## 实际验证记录

本轮固定源码基线为 `8a9dedf5e2357e5667cef5cbca2c3136b4526dcb`。原始独立克隆与应用完整补丁后的克隆分别验证，原版的预期业务失败没有计入修复版 PASS。完整补丁 SHA-256、运行挂载、原始日志、检查统计和两轮自审见随本轮变更交付的仓库外审核包；源码仍未提交。

本轮使用已安装的 `medical-acceptance:29d505f7-bcrypt401-r1` 依赖镜像、`pgvector/pgvector:pg16`、`redis:7-alpine` 和 `nginx:1.30.6-alpine3.24`，没有下载依赖或操作旧数据卷。已验证范围应以实际执行日志为准；从零构建、前端页面、Celery Worker、真实第三方服务、旧库升级均不在本轮已验证范围。
