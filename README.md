# Muse 微信管理台（Muse WeChat Manager）

在 Windows 本机连接 Muse（muse.ai）与微信 WeChat，通过微信 iLink Bot 通道收发消息，管理多组独立账号、浏览器和消息队列。每组连接一个 Muse 账号和一个微信 Bot 授权；这是实验性预览项目。

[下载便携包](https://github.com/such99/muse-wechat-manager-public/releases/tag/v0.1.0-preview-r11) · [操作手册](packaging/USER-GUIDE.md) · [版本记录](CHANGELOG.md) · [开发指南](CONTRIBUTING.md)

## 当前版本

| 内容 | 状态 |
| --- | --- |
| 已发布的 Windows 便携包 | **0.1.0-preview-r11**，包含全部恢复与登录修复、EXE、Python 与运行依赖 |
| main 分支源码 | **0.1.0-preview-r11**，与本次便携包的运行源码一致 |

r11 统一发布队列恢复与 Muse 登录确认修复。下载与旧安装处理见 [r11 发布说明](docs/RELEASE-r11.md)，已知限制见 [审查说明](docs/CLAUDE-REVIEW-FIXES.md)。

2026-10-16 补充：已修复 Muse 前缀 Cookie 导致的登录确认 HTTP 409，实例 2 已由使用者确认微信绑定和测试通过。原因与验证范围见 [登录修复记录](docs/LOGIN-409-FIX-2026-10-16.md)；本次 r11 下载包已包含此修复。

## 可以做什么

- 在中文本机管理台添加、命名、启动、停止和管理连接，预置六组独立配置。
- 转发本人 Muse 主聊天中的新增助手正文、图片和文件，保留消息顺序与去重记录。
- 为网页、提醒和操作卡片提供文字提示；过滤浏览器任务状态卡及空占位。
- 使用 Windows DPAPI 保存本机凭据，并核对账号、实例和浏览器进程归属。
- 可选接入 MiMo 辅助诊断和提出管理操作，执行前在本机确认具体目标。

微信授权是扫码者与 Bot 对话的 iLink 通道，**不接管普通微信的全部好友和群聊**。登录资格和授权范围以手机端实际提示为准。

## 下载与使用

1. 从 [r11 Release](https://github.com/such99/muse-wechat-manager-public/releases/tag/v0.1.0-preview-r11) 下载 ZIP，对照同版本的 [SHA256 校验值](distribution/SHA256SUMS.txt)，完整解压到可写目录。
2. 在 Windows x64 上双击 `Muse微信管理台.exe`，默认打开 `http://127.0.0.1:18700/`。需要已安装 Google Chrome 和系统 .NET Framework 4.x；便携包附带 Python 运行环境。
3. 按 [操作手册](packaging/USER-GUIDE.md) 在专用窗口登录本人 Muse、确认主聊天，再扫码授权并绑定本人微信。
4. 用手机完成文字、图片、文件收发核验。MiMo 为可选项，API Key 仅在本机软件中填写。

Release 另附 `manifest.json` 和便携包验证结果。ZIP、EXE 仅放在 Releases，源码仓库不包含使用后的运行目录。

## Muse 邀请码 / Invite code

> 🎁 **我的 Muse 邀请码：`SA7JX4`**
>
> 注册 Muse 需要邀请码的话，欢迎用我的这一个（App 里：设置 > Redeem token；网页版：设置 > General > Usage > Redeem invite code）。顺手帮个忙，谢谢！
>
> 🎁 **My Muse invite code: `SA7JX4`** — if you need an invite code when signing up for Muse, feel free to use mine. Thanks for the support!

## 使用限制与数据

- 依赖 Chrome 自动化及 Muse 页面和接口结构；页面改版、授权变化、平台规则或服务限制可能使功能失效。使用者应遵守相关平台要求。
- 网页和提醒目前以 `[网页]`、`[提醒/任务入口]` 等文字显示，**截图转发尚未实现**；语音也未完成支持。
- 程序不设累计发送条数上限，但实际微信接口仍可能拒收。提交或发送结果不明时暂停，需本人核对，不自动盲重发；iLink `-2` 的送达语义仍待真实平台验证。
- 未提交的排队文字和附件负载只在内存中；停止或重启后可能需要本人重发。r11 提供未提交项通知及需本人核对的恢复入口。
- **助手发送队列会在本机 SQLite 保存明文正文。** r10 默认开启正文日志；r11 新建实例默认关闭，已有配置不自动改变。DPAPI 只加密凭据，不加密全部聊天数据。
- `runtime/`、`secrets/`、`manager-data/`、日志和浏览器会话属于本机私人数据，保留到手动清理；不要上传或分享整个使用后的软件目录。

管理服务只监听本机回环地址，带 Host/Origin 校验；管理端另有会话 Cookie，写操作有确认保护。这些机制主要针对网页跨站调用，不能视为对同机恶意程序或同 Windows 用户进程的隔离。

## 验证与开发

r10 的 91 项回归和便携 EXE 检查通过；当前 main 包含登录修复的组合回归 **134 项通过**，测试使用假数据、离线或独立本机环境。此前 r7 修订实例已有使用者确认的文字、图片和文件反馈，本次实例 2 的修订安装另有微信绑定和测试通过反馈；这些反馈不替代 r10 发布包对应版本的真实验收。多账号真实并行、MiMo、授权过期与恢复等范围见 [测试报告](packaging/TEST-REPORT.md) 和 [验收矩阵](packaging/ACCEPTANCE.md)。

源码在 `real/`，构建与启动器在 `packaging/`，固定上游代码和许可在 `vendor/`，依赖锁为 `requirements-dev.lock.txt`。开发环境、测试命令和构建步骤见 [CONTRIBUTING.md](CONTRIBUTING.md) 与 [操作手册](packaging/USER-GUIDE.md#源码依赖与构建)。安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。

## 许可与项目关系

自有代码尚未授予 MIT、Apache-2.0 等开源许可证，公开可见不新增开源授权，详见 [RIGHTS.md](RIGHTS.md)。第三方组件保留原许可证，见 [第三方声明](THIRD-PARTY-NOTICES.md)。部分代码和文档由 ChatGPT/Codex 辅助生成和整理。

本项目不是 Muse、微信、MiMo 或 OpenAI 的官方产品。
