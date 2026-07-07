# Changelog

本文件记录 Agent Bridge 项目的所有重要变更，格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

### Added
- 新增 `PUT /api/agents/{agent_id}` 单 Agent 更新端点，替代"改一个 Agent 要全量 POST"的反模式
- 新增 `POST /api/rooms/{room_id}/resume` 端点，明确支持 error/paused → running 的恢复路径
- WebUI 引入 hash 路由（`#/chat`, `#/chat/<room_id>`, `#/agents`, `#/settings`），刷新不丢视图、支持浏览器后退
- WebUI 消息渲染改为 diff 增量更新，消除每 5 秒整体淡入闪烁
- WebUI 按当前路由精准轮询（设置页不打任何轮询、会话视图才打消息/日志/turn）
- WebUI 统一 Modal/Toast/Confirm 组件，全部支持 Esc/点遮罩关闭
- WebUI 事件委托模型（document 单点监听 + data-action 分发），替代 inline onclick + 全局函数
- WebUI 发送消息支持乐观本地插入
- WebUI 删除 Agent 时引导处理（检测到运行中房间会提示前往聊天室，而非硬阻塞）
- WebUI 配置 banner 显示全部 issues，不再截断
- 创建 pyproject.toml，支持标准 Python 打包（pip install -e .）
- 创建 CHANGELOG.md 变更日志
- 新增 runtime.py 单元测试（39 个测试用例）
- 新增 settings 页面（端口/数据目录/轮询间隔/日志设置/关于）
- 新增 GET/PUT /api/settings API 端点
- 添加 CSP 安全头和 X-Content-Type-Options 到所有 HTTP 响应
- 添加 frame-ancestors 'none' 防止 iframe 嵌入
- 拆分 index.html 为 style.css + app.js + index.html（主 HTML 从 5323 行精简到 163 行）

### Changed
- **WebUI 全量重写**：index.html / style.css / app.js 三件套从零重写（5530 行 → 2841 行，减 49%）
- CSS 删除双套主题叠加的技术债（原 2713 行含 1600 行死代码），统一到夜林/苔绿 token 体系
- 视觉重做：基于 STYLE_GUIDE 色系重新设计视觉语言，圆角统一（8/6/4）、阴影极简（单层）、动效统一（150ms cubic-bezier）
- 暗色模式：修复 STYLE_GUIDE.md 的 `:root.light` 选择器 bug，独立配色（非亮度翻转）
- 房间状态视觉强化：running/waiting/paused/error 四态用明确色 + 点状指示
- 对比度修复：text3 从 4.0:1 提升到 4.6:1（亮色）/ 4.7:1（暗色），符合 WCAG AA
- 统一 V1/V2 调度路径，移除 V1 fallback，runtime.py 成为唯一调度路径
- 拆分 server.py（2000 行）为 config.py / discovery.py / poll_manager.py / routes.py / server.py（入口 180 行）
- 更新 protocol/SPEC.md 为完整的 V2 协议文档（826 行）
- rooms.py 的 tick_room() / tick_running_rooms() 标记为 @deprecated

### Fixed
- 修复主题切换按钮图标逻辑绕的问题（重写为简单的 dark/light 互切）
- 修复测试 Agent 模态不支持 Esc 关闭（统一组件后天然支持）
- 修复轮询指示点动画在第二套主题里被关闭的问题
- 修复嵌套 em 字号难以预测的问题（统一为 px 字号 token）
- install.sh BRANCH 从 main 改为 dev，与 install.ps1 保持一致
- 清理 test_runtime.py 中未使用的导入

### Removed
- 删除 CSS 双套主题叠加（玫红命令中心风 + 苔绿极简风并存的技术债）
- 删除 inline onclick 全局函数事件模型
- 删除两套并行的 Agent 编辑入口（badge popup vs card 表单），统一为卡片表单
- 删除硬编码的 settings HTML（改为 JS 根据 API 动态生成）

## 2026-05-25 — Adapter V2 UI 与联调优化

### Added
- 支持 Adapter V2 UI 配置界面
- 添加版本现状与优化清单文档及可视化 HTML

### Changed
- 优化 Agent 配置体验
- 优化 Agent 新建和联调体验
- 优化 OpenClaw 联调提示
- 优化保存成功提示

### Fixed
- 修复 dev 安装脚本更新 UI 的问题
- 本地化适配器配置标签
- 自动创建测试聊天室
- 标记并清理测试房间

## 2026-05-23 — 架构改造：EventBus / Adapter V2

### Added
- 完成架构改造：引入 EventBus 事件总线
- 引入 Adapter V2 适配器体系
- 引入 Runtime 运行时管理
- 引入 Scheduler 调度器
- 引入 MCP Server 支持
- 引入 Skill 技能系统
- 引入 Callback 回调机制

### Fixed
- 修复 6 个硬问题 + 21 个集成测试
- 收尾 5 项修复 + 3 个集成测试

## 2026-05-22 — 适配器调试与品牌体验

### Added
- 接入品牌图标
- 增加响应体调试日志，记录适配器返回的原始响应内容
- 增强运行日志细节并添加复制按钮

### Changed
- 统一 Agent 卡片操作按钮
- 统一 Agent 卡片按钮尺寸
- 同步 Agent 数量与按钮样式
- 优化 Agent 管理保存交互
- 优化聊天室卡片新增入口
- 优化聊天室进入和运行提示
- 优化聊天室卡片样式

### Fixed
- 捕获适配器响应体并自动写回同步回复

## 2026-05-21 — WebUI 视觉改版与 Agent 管理

### Added
- 聊天室新增运行时日志功能
- 聊天室与 Agent 关联校验

### Changed
- 重设 WebUI 视觉风格
- 优化 Agent 页面扫描体验
- 优化 Agent 删除确认和扫描目录入口
- 调整 Agent 页面布局并刷新聊天 Agent 列表
- 提交工作区未提交更改

### Fixed
- 优化运行日志展示
- 优化 Agent 配置表单
- 移除默认示例 Agent
- 优化 Agent 扫描密钥处理

## 2026-05-20 — UI 侧边栏重构与安装修复

### Added
- 支持通用房间通道与适配器抽象
- 聊天页重构为聊天室模式，移除「只接收来信」配置项
- UI 重构为基于侧边栏的应用外壳布局
- 房间日志迁移至对话视图
- 发现本地 Agent 功能
- Agent 配置从设置页移至独立 Agent 标签页
- 设置页共享目录增加「打开目录」按钮

### Changed
- 优化布局、排版、交互和动画
- 使用横向卡片网格 + 图标展示 Agent
- 调整 Agent 卡片网格以容纳 3-4 张卡片
- 统一设置页「打开目录」按钮样式

### Fixed
- 修复安装脚本中 curl|tar 管道改为两步下载，解决代理环境下管道断流导致安装失败的问题
- 代码审查修复 6 处问题（2 Critical + 2 Warning + 2 Suggestion）
- 修复审查发现的三个问题
- 修复 install.sh 安装完成提示的颜色转义乱码
- 修复 Hermes Agent 自动发现时 webhook 401 认证失败
- 修复扫描功能：移至 Agent 页并修复卡片嵌套，清理死代码
- 将 Agent 发现从自动扫描改为设置页手动按钮
- 移除首次启动时的自动 Agent 发现
- 优化聊天室列表展示
- 聊天室卡片模型行支持换行
- 避免聊天页房间列表闪烁
- 防止房间卡片模型名称溢出
- 保留房间撰写选择状态
- 打开当前聊天文件
- 打开当前聊天文件夹
- 默认优先使用已发现的 Agent

## 2026-05-19 — Agent 发现与聊天室重构

### Added
- Agent 卡片增加「测试对话」按钮，验证 Webhook 连通性
- 新增聊天室弹窗改为自定义模态框，替代浏览器原生 prompt
- 聊天页重构为聊天室模式
- Agent 配置从设置页移至独立标签页
- 兼容全局版本参数
- 静默安装 Python 依赖

### Changed
- 测试对话改为模态框展示，增加加载动画和成功/失败样式
- 隐藏 pip 版本升级提示

### Fixed
- 测试对话错误提示分类：区分服务未启动/端口错误/超时/DNS/HTTP 错误
- 测试对话按钮无反应，补全缺失的 toast 函数并增加字段校验高亮
- 根据 gateway.auth.mode 动态推断 JSONPath
- 修正 token_jsonpath 为 gateway.auth.token
- OpenClaw 发现时 token_path 展开为 Windows 绝对路径

## 2026-05-18 — WebUI 功能增强

### Added
- 为 WebUI 添加明暗色模式
- 取消首次 setup 配置阶段
- 添加用户完整使用流程文档
- 重塑项目娱乐化定位文档

### Fixed
- 修复消息投递可靠性和跨平台问题

## 2026-05-16 — 安装脚本与文档完善

### Added
- 添加 bridge uninstall 卸载命令
- 添加从零到上手指南 GETTING_STARTED.md
- README 添加安装前提、uninstall 命令、Windows 指引

### Changed
- 安装脚本输出改为英文，改进网络错误提示
- 安装脚本去掉 git 依赖，改为 curl/Invoke-WebRequest 下载压缩包
- Windows 安装说明区分 PowerShell 和 cmd

### Fixed
- 修复 exit 导致 PowerShell 窗口关闭的问题
- 修复 Windows 安装命令（去掉多余的 powershell -c）
- 明确提示 GitHub 连接失败和解决方法

### Removed
- 去掉安装脚本的 git 依赖

## 2026-05-15 — 项目初始化与核心基础设施

### Added
- 项目初始化：Agent Bridge — 通用异步 Agent 通信中间件
- 新增 CLI 工具和一键安装脚本
- UI 从聊天框升级为控制中心
- 设置页从纯文本升级为可视化表单编辑器
- 轮询集成到 UI 服务，一个命令启动一切
- 新增 PRINCIPLES.md 项目开发原则文件
- 完善并发安全、最小修改、归档边界、改后自检等原则

### Changed
- 重写 PRINCIPLES.md：纯业务约束，去掉所有代码和技术细节

### Fixed
- README 对齐原理图并简化快速开始流程
- 锁机制统一、游标重置、命令注入、文档同步
- 二轮代码审查修复
- 全面代码审查修复
- file:// 协议下显示友好提示而非 CORS 报错
- 修复设置页无法加载：esc() 函数未定义
- 全面修复与改进
