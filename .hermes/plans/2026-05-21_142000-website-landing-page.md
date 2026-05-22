# Agent Bridge 官网设计与上线计划

## 目标

为 agent-bridge 设计一个极简实用技术风的单页官网，部署到 `agentbridge.cymrise.cn`（cymrise.cn 子域名）。

## 当前上下文

- **项目**: Agent Bridge — 轻量 AI Agent 异步对话剧场
- **定位**: 娱乐导向、实验导向的轻量工具，基于共享文件的 JSONL 消息传递
- **核心卖点**: 透明、可观察、可手工干预、低成本、剧情感
- **现有资产**: GitHub README（中文，内容完整）、本地 WebUI（非公开）、无公网落地页
- **参考部署**: MomoPet 官网 (`momopet.cymrise.cn`) — 纯静态 HTML + 阿里云 ECS + OpenResty 反代 + `*.cymrise.cn` 通配 SSL

## 设计约束（来自用户要求）

- **极简实用技术风**，服务信息传递效率，弱化视觉装饰
- **低饱和冷色系、黑白灰为主**，少量浅色点缀，禁止高饱和撞色/渐变
- **无衬线字体**，文字层级分明，代码区等宽字体
- **单页流式竖向布局**，核心内容前置
- **交互克制**: 无弹窗、无动画转场、仅滚动/复制/跳转
- **桌面端优先**，兼顾移动端自适应
- **文案精简**，短句，适合扫读
- **独立 HTML 文件**，零依赖，无构建步骤

## 页面结构（从上至下）

### 1. 导航栏（固定顶部）
- 左: Logo 文字 "Agent Bridge"
- 右: GitHub 源码链接、快速开始锚点、文档链接（README）

### 2. Hero 区
- 一句话定位: "轻量 AI Agent 异步对话剧场"
- 副标题: 15 字以内的功能概括
- 两个按钮: "快速开始"（锚点到安装区）+ "查看源码"（GitHub 链接）
- 一段伪代码/流程图示意（极简 ASCII 或内联 SVG，展示 Agent A → JSONL → Agent B 的核心机制）

### 3. 核心特性（3-4 个卡片）
每个特性: 短标题 + 一句释义，无冗余

- **文件即舞台** — active.jsonl 就是正在发生的对话，直接可读可编辑
- **Webhook 唤醒** — 轮询新消息后通过 webhook 叫醒目标 Agent
- **本地控制台** — WebUI 实时旁观对话、管理角色、发送消息
- **剧情感管理** — 归档旧章节、开新幕、暂停/恢复

### 4. 安装与快速开始
- 三平台安装命令（macOS/Linux curl、Windows PowerShell、Windows cmd）
- 三行常用命令（start、send、status）
- 所有代码块配一键复制按钮

### 5. 工作原理（极简）
- 文字 + 代码块展示核心流程（共享目录结构 + 5 步流程）
- 直接复用 README 中的结构图和流程说明，精简措辞

### 6. 配置预览
- 最小 bridge.yaml 示例（精简版，展示核心字段即可）
- 关键字段说明表格

### 7. 页脚
- MIT License 标识
- GitHub 链接
- "由 SusuAgent 构建" 或类似

## 文件变更

| 文件 | 操作 | 说明 |
|------|------|------|
| `website/index.html` | 新建 | 官网单页 HTML，内联 CSS/JS |
| `website/icons/` | 新建 | favicon 等图标资源（按需） |

## 部署方案

参照 MomoPet 的方式：

1. **静态文件**: `website/index.html` 存入 agent-bridge 项目的 `website/` 目录
2. **服务器**: 在阿里云 ECS（47.95.192.149）上配置
3. **DNS**: 在 cymrise.cn 的 DNS 添加 A 记录 `agentbridge` → `47.95.192.149`
4. **OpenResty**: 新增 server block，监听 `agentbridge.cymrise.cn`，root 指向静态文件目录
5. **SSL**: 复用 `*.cymrise.cn` 通配证书
6. **上传**: scp 静态文件到 ECS 对应目录

由于 agent-bridge 没有自己的后端服务器在 ECS 上运行，部署方式比 MomoPet 更简单 — 纯静态文件直接由 OpenResty/Nginx 托管，不需要反代到应用层。

## 技术实现细节

- **单一 HTML 文件**，内联 `<style>` 和 `<script>`
- **零外部依赖**: 无 CDN 引用、无框架、无字体加载（使用系统字体栈）
- **暗色主题默认**（技术工具调性）+ 可选亮色切换
- **代码复制**: 原生 JS `navigator.clipboard.writeText()`
- **响应式**: CSS media query 处理移动端，桌面端 max-width 限制内容宽度
- **性能**: 无外部资源请求，首屏 < 50KB，加载 < 200ms

## 验证步骤

1. 本地浏览器打开 `website/index.html` 检查布局和内容
2. 浏览器 DevTools 模拟移动端尺寸验证响应式
3. Lighthouse 跑分确认性能和可访问性
4. 部署后访问 `https://agentbridge.cymrise.cn` 确认 HTTPS、DNS、页面正常

## 风险与待确认

| 项目 | 说明 |
|------|------|
| ECS 服务器访问 | 需要艺萌提供 SSH 访问方式或自行配置 OpenResty |
| DNS 配置 | 需要在域名管理面板添加 A 记录 |
| 内容准确性 | 页面文案需基于 README 精简，上线前由艺萌审阅 |
| 项目图标/Logo | 目前没有，先用纯文字 Logo |
