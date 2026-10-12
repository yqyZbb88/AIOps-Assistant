# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

面向少量 Linux 主机的运维人员；普通用户查看获授权主机和私有会话，审批身份决策可见申请，管理员维护连接资源、身份与配置。角色边界以服务端为准。

## Product Purpose

把 Prometheus 指标、Alertmanager 告警、受控 SSH 诊断、人工审批与 SQLite 历史组织在同一控制台，帮助用户确认异常并完成受控处置。

## Operating Context

原生 HTML/CSS/JavaScript 单页控制台由 FastAPI 提供 `/console` 和本地静态资源。七个视图：总览、主机、问答诊断、审批、运维记录、系统管理、个性化。顶部主机选择影响详情、会话选择、趋势与记录；既有会话固定主机。

## Capabilities and Constraints

- 单进程、单 worker、SQLite；当前不支持多实例共享配置。
- 同源 API、Bearer Token 鉴权与服务端主机授权；Token 仅保存于当前标签页 sessionStorage。
- 固定指标目录与真实时间采样；不开放自由 PromQL、SSH 地址或 Shell。
- 规则自动处置与 Agent 人工审批为独立路径，界面重构不得改变执行或授权机制。
- 前端显示偏好保存在 localStorage，包括主题、密度、默认页面、刷新、收藏和总览组件顺序/显隐。
- 沿用原生前端与渐进迁移；公共视觉、系统管理与安全 Markdown 展示逐批改造，不增加框架和大型依赖。会话搜索、会话列表布局与滚动策略仍沿用现有行为。

## Brand Commitments

沿用 AIOps-Assistant 名称。用户指定清晰、高效、可靠、现代的专业运维管理平台方向，避免过度装饰和无意义动画。

## Evidence on Hand

事实依据：README.md、docs/architecture.md、docs/console.md、src/web/console.html、console.css、console.js 和既有测试。运行数据必须来自真实接口；不能为视觉效果伪造监控样本或正常状态。

## Product Principles

- 缺失、失败与真实零分别表达。
- 明确全部授权主机与当前主机的显示范围。
- 保留业务功能、安全边界与用户操作流程。
- 优先复用现有组件和技术栈，分阶段验证。
