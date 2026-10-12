---
name: AIOps-Assistant
description: 清晰、高效、可靠的专业运维控制台；记录公共视觉、运行总览、系统管理和安全 Markdown 的已实现规范。
colors:
  dark-bg: "#071017"
  dark-panel: "#0d1922"
  dark-panel-2: "#111f2a"
  dark-panel-3: "#162733"
  dark-line: "#223640"
  dark-line-strong: "#345463"
  dark-text: "#edf4f5"
  dark-muted: "#8ca0aa"
  dark-accent: "#62dbc7"
  dark-accent-soft: "#143b38"
  dark-on-accent: "#041411"
  dark-blue: "#78aef8"
  dark-amber: "#efb35e"
  dark-red: "#ff7474"
  light-bg: "#f3f5f6"
  light-panel: "#fff"
  light-panel-2: "#f7f9fa"
  light-panel-3: "#edf1f2"
  light-line: "#d3dfdf"
  light-line-strong: "#a7c2c0"
  light-text: "#132126"
  light-muted: "#566b73"
  light-accent: "#087e70"
  light-accent-soft: "#d7efeb"
  light-on-accent: "#fff"
  light-blue: "#2869ba"
  light-amber: "#9b620b"
  light-red: "#bb303a"
typography:
  headline:
    fontFamily: 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif'
    fontSize: "1.5rem"
    fontWeight: 700
    letterSpacing: "-.025em"
  title:
    fontFamily: 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif'
    fontSize: "1.125rem"
    fontWeight: 700
  body:
    fontFamily: 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif'
    fontSize: "14px"
    lineHeight: 1.5
  label:
    fontFamily: 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif'
    fontSize: ".8125rem"
  reading:
    fontFamily: 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif'
    fontSize: ".9375rem"
    lineHeight: 1.7
  subheading:
    fontFamily: 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif'
    fontSize: "1rem"
  stat:
    fontFamily: 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif'
    fontSize: "1.625rem"
    fontWeight: 650
    letterSpacing: "-.025em"
rounded:
  control: "8px"
  panel: "12px"
  overview-badge: "5px"
spacing:
  space-1: ".25rem"
  space-2: ".5rem"
  space-3: ".75rem"
  space-4: "1rem"
  space-6: "1.5rem"
components:
  button-primary:
    rounded: "{rounded.control}"
    padding: ".66rem .9rem"
  button-secondary:
    rounded: "{rounded.control}"
    padding: ".66rem .9rem"
  button-text:
    padding: ".3rem"
  input:
    rounded: "{rounded.control}"
    padding: ".68rem .75rem"
  navigation:
    rounded: "{rounded.control}"
  overview-badge:
    rounded: "{rounded.overview-badge}"
    padding: ".17rem .45rem"
  overview-panel:
    rounded: "{rounded.panel}"
    padding: "{spacing.space-4}"
---

# Design System: AIOps-Assistant

## Overview

**Creative North Star: "运行值守台"**

沿用已经确认的专业运维方向：清晰、高效、可靠、现代。公共壳层保留深浅主题、青绿强调色和系统字体；首批运行总览以紧凑摘要、平面面板和细分隔线组织信息，让用户先确认主机、异常与待办，再进入原有诊断和处置流程。这里的名称描述既有方向，不引入新的品牌或视觉世界。

本规范依据 `src/web/console.css`、`console.html`、`console.js` 与 `markdown.js` 的实际实现，产品约束见 `PRODUCT.md`。通用面板与审批卡采用平面表面，静态英文装饰眉题已移除；系统管理使用分组导航、紧凑配置状态和就近反馈。Markdown 展示独立复用。会话管理布局、搜索和滚动策略不属于本批；不能将公共视觉统一等同于所有页面交互已重构。

**Key Characteristics:**

- 深浅主题共用语义变量，青绿强调色用于操作、选中和已确认的良好状态。
- 首批总览平面、紧凑，摘要、对象列表和趋势各有明确层级。
- 未知、部分失败、真实零分别表达；范围与刷新信息就近可见。
- 沿用原生控件、线性 SVG、键盘焦点和现有密度偏好。

## Colors

深色为冷静的深蓝中性底与浅青绿强调，浅色为清浅中性底与深青绿强调。前置 YAML 是色值的规范来源，运行时通过 CSS 语义变量切换；不要把某一主题的常量写进跨主题组件。

### Primary

- `dark-accent` / `light-accent` 对应 `--accent`：主操作、选中标记、趋势线和已确认的良好状态。
- `dark-accent-soft` / `light-accent-soft` 对应 `--accent-soft`：导航选中、次级操作悬停与轻量选中底色。
- `dark-on-accent` / `light-on-accent` 对应 `--on-accent`：主按钮、品牌标记、跳转链接的强调色底上前景。必须随主题切换。

### Neutral

- `*-bg` 对应 `--bg`：页面底、顶栏；`*-panel` 对应 `--panel`：侧栏与总览主面板。
- `*-panel-2`、`*-panel-3` 对应同名语义变量：输入、趋势容器和辅助层次。用表面差异保持分组，不靠堆叠卡片制造层级。
- `*-line` / `*-line-strong` 对应 `--line` / `--line-strong`：常规细分隔与强调边界。
- `*-text` / `*-muted` 对应 `--text` / `--muted`：主内容与次级说明。浅色次级文字已经提高对比度，后续迁移继续使用变量。

### Status

- `*-amber` 对应 `--amber`：警告、待审批和不完整查询；`*-red` 对应 `--red`：失败与不可达。
- `*-blue` 对应 `--blue`：保留现有辅助色变量。首批总览没有增加第二套强调色或新配色。
- 未确认数据使用明确文字和破折号；既有主机状态徽标可带失败色，但颜色不能替代“未确认”“查询失败”等标签。

**The 主题语义 Rule.** 组件使用 `var(--accent)`、`var(--on-accent)` 等现有语义变量；深浅主题必须成对检查。

## Typography

**Body Font:** 系统无衬线字体，中文回退至 Microsoft YaHei；完整栈见 YAML。没有远程字体或装饰字体。

**Label/Mono Font:** 标签、说明与徽标使用正文家族；代码、图轴和技术数据使用 `ui-monospace, monospace`。等宽字体不用于装饰标题。

**Character:** 以任务识别和数值扫描为主。正文、标签、面板标题和顶栏标题分层，保留自然中文阅读节奏；不使用宣传性超大标题占据总览首屏。

- **Headline**：顶栏视图标题（24px），使用 YAML 的 `headline`。
- **Title**：总览“运行态势”标题（18px），使用 `--text-title`；总览面板标题沿用全局 `h2`（1.12rem，约18px）。
- **Body**：正文（14px，行高1.5）。
- **Label**：总览范围、面板说明与摘要标签（13px），使用 `--text-label`。
- **Reading**：Markdown 正文（15px，行高1.7），最长75ch；消息内标题使用18px与16px两档，代码使用13px等宽字体。
- **Stat**：总览摘要数值（26px），使用 YAML 的 `stat`；次级详情沿用较小正文。
- 摘要数值、主机指标、区间值和图轴使用 `tabular-nums`；主机名称、ID、错误详情允许换行。

## Layout

桌面壳层是侧导航加主区：展开侧栏（250px）、收起侧栏（74px），顶栏保留当前主机、刷新和连接操作。首批壳层已去掉顶栏和侧栏背景模糊，并保留侧栏展开按钮与可访问名称。

首批总览从紧凑介绍与原有入口开始，随后是范围/刷新上下文、四列连续摘要、主机与关注项双栏；收藏趋势和最近动态横跨内容区。双栏比例为 `minmax(0, 1.1fr) minmax(0, 1fr)`。摘要放在一个共同面板内，用细线分隔，避免每个数字再包一层独立卡片。

公共间距阶梯为（4/8/12/16/24px），对应 YAML 的 `space-1/2/3/4/6` 与同名 CSS 变量。总览面板常规内边距使用 `space-4`，紧凑密度使用 `space-3`；主机行紧凑密度减至 `space-2` 的垂直内边距。继续保留舒适/紧凑偏好。

- 宽度不超过（1100px）：摘要改为两列；既有分栏页面也有相应收拢规则。
- 宽度不超过（780px）：总览主区单列，顶栏单列排列；侧导航移到底部，收起偏好不隐藏手机导航文字。导航、图标按钮与总览按钮提供至少（44px）触控高度，底栏和主区预留 `env(safe-area-inset-bottom)`。
- 宽度不超过（520px）：总览内边距使用 `space-3`；介绍区动作同一行等分可用宽度，摘要保持两列。
- 页面最小宽度（320px）。趋势网格使用可收缩最小值，长标识和错误信息换行，避免内容撑破容器。
- 根滚动区为桌面顶栏与移动端底栏预留滚动空间，保证键盘聚焦的表单控件不会被固定导航遮挡。

范围标签不可省略：主机与活动告警面向全部授权主机，审批面向当前身份可见申请；趋势面向当前主机。最近动态有当前主机时显示该主机，已连接但未选择主机时明确标为全部授权主机的历史记录。公开就绪探针独立于身份连接。

## Elevation & Depth

首批总览在静止与悬停时都以中性表面和细边界分组：主面板无渐变、无阴影；趋势使用次级表面，主机行通过轻量底色反馈悬停。公共顶栏、侧栏和品牌标记也已采用纯色。

通用面板和审批卡均使用纯色、细边界，不使用常驻投影。通知、弹出菜单和原生对话框保留浮层阴影；浅色底采用更中性的灰白表面。强调色仅承担操作、选中与语义状态。

**The 平面表面 Rule.** 共享面板使用中性表面与细边界；只有需要明确浮层关系的通知、菜单、对话框使用阴影。

状态变化沿用短时颜色/边界过渡；已移除视图与消息的装饰进场动画，保留发送等待时的打字反馈。`prefers-reduced-motion: reduce` 关闭动画、过渡和平滑滚动。

## Shapes

公共控件使用克制圆角（8px），首批总览外层面板使用较柔和圆角（12px）；分别由 `--radius-control`、`--radius-panel` 表达。连续摘要只在外容器使用圆角，内部单元保持直边与分隔线。

总览状态徽标使用轻圆角（5px）和浅色边界；既有其他视图仍有胶囊徽标、快捷诊断 chip、趋势卡及对话框的不同圆角。保留这些真实差异，不能把总览圆角规则说成已经覆盖所有页面。

## Components

### Buttons

操作直接、可辨识。主按钮使用强调色底与 `--on-accent` 前景；次按钮透明底、主文字；文字按钮无边界、强调色文字。内边距见 YAML，主/次按钮沿用控件圆角。悬停显示强调边界，次按钮、文字按钮与图标按钮使用淡强调底色；禁用态降低不透明度并显示不可用光标。

使用原生 `button`，普通操作明确 `type="button"`；表单提交保留 `submit`。图标操作有中文 `aria-label` / `title`，装饰 SVG 使用 `aria-hidden="true"`。按钮、链接、输入、选择器、文本域及折叠摘要都有可见键盘焦点：强调色轮廓（2px）及外偏移（2px）；主机行焦点内收以适应连续列表。

表单内普通操作按内容确定宽度，不继承纵向 flex 的整行拉伸。凭据卡用独立动作组和8px间距分隔按钮，标题与操作区之间保留12px间距。工具栏允许换行；标题可收缩和断词，图标动作保持固定尺寸。发送按钮使用普通44px高度，不随多行输入框拉高；手机排序、表单及弹窗按钮保留至少44px触控高度。

### Inputs / Fields

次级表面、细线边界、控件圆角；字体继承正文，标签说明使用13px次级文字。保留原生 `label`、`input`、`select`、`textarea`，输入光标使用强调色。对话框有标题关联、关闭操作、最大可视高度和内部滚动边界。SSH 必填校验就近显示错误并聚焦字段；服务端配置错误保留完整说明。管理草稿在失败时保留，关闭页面提供未保存提示。

复选项使用左文右控件的横向整行标签：说明左对齐并可换行，复选框保持约18px原生图形与主题强调色，整行至少44px。普通文本字段的100%宽度和纵向标签规则必须排除checkbox/radio及开关行；不得用额外的 `!important` 掩盖这类覆盖。个性化在1100px以下使用单列面板，520px以下字段标签与控件也改为单列。

### Navigation

公共导航采用一致的线性内联 SVG（1.2rem，描边1.65）、中文文字和可访问名称。选中项使用淡强调背景、主文字及强调色标记，通过 `aria-current="page"` 声明当前页面。收起桌面导航仍可再次展开；手机恢复文字与计数。跳到主要内容链接在键盘聚焦时可见。

### Chips / Badges

总览状态徽标配文字表达可达、不可达、查询失败、未确认等状态，颜色仅为辅助。状态边界由当前文字色与 `--line` 混合得到，圆角见 YAML。旧视图的胶囊徽标和可点击诊断 chip 仍保留；状态标签不能伪装成可操作按钮。

### Cards / Containers

共享面板使用 `--panel`、常规细线和面板圆角，无渐变/阴影。总览头部细分隔把标题/范围与数据主体分开。趋势与技术内容使用 `--panel-2` 和控件圆角。管理参数分组使用上边分隔和留白，不再套虚线边框。

### Management / Markdown

管理页先呈现系统范围、草稿／磁盘／运行状态与保存动作，再呈现三组导航。九个分区保留单一键盘序列；手机分组标题与三个按钮同排，按钮至少44px。安全与自动处置面板自然高度，避免短内容被拉成空白大卡片。独立分区隐藏全局保存，凭据失败提供明确重载入口，敏感操作使用原生确认对话框。

Markdown 的历史与新回复共用 DOM 白名单组件。表格与代码块自身滚动且可聚焦；复制状态就近反馈，权限拒绝时显示手动复制文本。消息内不执行 HTML、外部图片、样式或代码；异常与超限回退完整纯文本。详细格式、链接与上限见 `docs/console.md`。

### Overview States / Trends

未知状态显示破折号和原因；成功查询得到的零才可写为零。部分主机告警未确认时显示未确认台数；若已有活动告警则数值带 `+`，没有已知活动告警时仍显示未知占位。未连接、加载、查询失败、无主机、无活动项分别给出真实说明；不因空数组推断监控正常。

主机行与关注项通过原生按钮进入已有主机/审批流程，定时刷新保留仍存在的同一操作焦点。刷新期间禁用重复刷新；切换主机或范围立即清除旧趋势并显示加载态，迟到响应受代际检查保护。

趋势使用真实采样、时间、单位和区间统计；无数据与不适用分别表达。趋势查询失败先显示简短恢复提示，技术错误收在默认折叠的 `details` 中，通过“查看错误详情”展开。设计文档与组件预览不提供虚构正常曲线或监控样本。

## Do's and Don'ts

### Do:

- **Do** 使用现有语义变量并检查深浅主题，强调色底上使用 `--on-accent`。
- **Do** 在首批总览保持平面面板、紧凑层级、连续摘要与细分隔线。
- **Do** 就近标明全部授权主机、当前主机、当前身份可见申请与刷新状态。
- **Do** 区分未知、部分失败、真实零、无数据和不适用，保留恢复入口与折叠技术详情。
- **Do** 保留原生控件、键盘焦点、手机导航文字、安全区和 reduced-motion 行为。

### Don't:

- **Don't** 把未完成查询、不可达或空响应显示成已确认正常或真实零。
- **Don't** 在切换主机后继续显示上一主机趋势，或把历史告警混成实时关注项。
- **Don't** 为首批总览重新加入宣传性大标题、渐变面板、重阴影或装饰动画。
- **Don't** 引入新字体、新配色、框架或大型依赖来替代现有系统。
- **Don't** 把公共样式统一当作所有视图交互已完成重构，或把尚待验证的全站无障碍与业务行为写成已验收事实。
