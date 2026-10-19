# 医疗患者端

基于 Vue 3、TypeScript、Vite、Vue Router、Element Plus 和 Axios，提供登录/注册、科室与医生浏览、预约、病历、处方选药、账单、检查报告及 AI 辅助分诊页面。整体架构与功能见[项目首页](../README.md)。

## 本地开发参考

在本目录执行；Node.js 版本需满足锁定依赖要求：

```powershell
npm ci
$env:BACKEND_PROXY_TARGET = "http://127.0.0.1:8001"
npm run dev -- --host 127.0.0.1 --port 3001 --strictPort
```

`/api` 由 Vite 代理至 `BACKEND_PROXY_TARGET`；代理目标与前端监听端口需分别配置。后端、数据库和合成账号需单独准备，不要在前端存放服务端密钥。

患者端与后台分别保存 Token。本端的页面入口不能替代后端的患者归属检查。`npm run build` 执行类型检查与生产构建，不等于完整业务 E2E。

## 验收边界

阶段 C 的真实浏览器冒烟覆盖合成患者登录、身份加载和查看已完成预约，关键接口返回 200。[实际截图](../docs/screenshots/client-business-smoke.png)。处方选药、药费账单及越权拒绝等通过真实后端 HTTP 和 PostgreSQL 检查，不意味着这些页面全部做过浏览器交互验收。

AI 页面有对应源码，但本轮未验证真实模型/检索质量、完整医疗诊断或线上支付回调。详见[首页验收结果](../README.md#独立环境验收结果)及[项目使用说明](../README.md#项目使用说明)。
