# Muse 微信管理台

Windows x64 本机管理软件：每组连接一个 Muse 账号和一个微信 Bot 授权，凭据、浏览器与消息数据分别隔离。MiMo 可辅助诊断和提出管理操作，执行前仍须在本机确认。

当前已发布便携版本为 **0.1.0-preview-r10**。main 分支另有 **r11 审查候选**：修复提交锁恢复、重启丢失提示和生成暂停，尚未包含在 r10 ZIP 中，详见 [审查修复与剩余问题](docs/CLAUDE-REVIEW-FIXES.md)。此仓库使用独立的干净历史。

## 下载与使用

1. 从 [r10 Releases 页面](https://github.com/such99/muse-wechat-manager-public/releases/tag/v0.1.0-preview-r10) 下载 Windows x64 便携 ZIP，完整解压到可写目录。
2. 双击 `Muse微信管理台.exe`，默认打开 `http://127.0.0.1:18700/`。
3. 按 [操作手册](packaging/USER-GUIDE.md) 完成 Muse 登录、微信扫码、绑定与手机收发核验。需要已安装 Google Chrome；便携包附带 Python 和运行依赖。
4. MiMo 为可选功能，API Key 只在本机软件的“配置 API”中填写。

ZIP、manifest 与便携包验证结果放在 Releases。仓库 `distribution/` 只保存 [SHA256SUMS.txt](distribution/SHA256SUMS.txt)。下载后可用 `Get-FileHash .\MuseWeChatManager-0.1.0-preview-r10.zip -Algorithm SHA256` 对照同一版本的校验值。

## 仓库内容

| 路径 | 内容 |
| --- | --- |
| `real/` | Python 服务、HTML/JavaScript 界面与管理器测试 |
| `packaging/` | 构建、EXE 验证、启动器源码、手册与验收文档的唯一维护源 |
| `vendor/` | 两个 MIT 上游项目的所需源码与许可证 |
| `instances/` | 六份未登录、自动启动关闭的示例配置 |
| `requirements-dev.lock.txt` | 固定开发依赖版本 |
| `licenses/` | Python 与生产依赖许可证、依赖元数据 |
| `distribution/` | 当前便携包 SHA256 校验值，不提交 ZIP 或 EXE |
| `docs/` | 公开发布计划、上传检查与许可研究说明 |

构建脚本从 `packaging/` 复制手册到便携包根目录，方便直接阅读；这些是生成的分发副本，源码仓库不再保留第二份。Python 和第三方运行库完整文件只随 Releases ZIP 分发。

## 版本与验证范围

r10 同步两批本机修复：2026-10-13 的任务状态卡及空占位过滤、网页与提醒的可读文字提示，以及 2026-10-14 的取消应用累计发送次数门槛。浏览器任务卡带附件或正式说明时也不会被误判为助手生成失败；普通正文、图片、文件和真正的失败提示继续保留。网页与提醒目前显示 `[网页]`、`[提醒/任务入口]` 等文字，截图转发尚未实现。

程序不设累计发送条数上限；实际微信接口拒收时保留队列，发送结果不明时暂停并要求人工核对，仍不自动重发。首次连接或桥接重启后可能需要微信发一条消息初始化会话。

r10 转发相关 **58 项**及管理器 **33 项**测试通过，共 **91 项**。其中包含 JavaScript 核心 33 组及过滤 39 组检查，不重复计入总数。均为假凭据、离线模拟或独立本机进程测试；便携包逐项验证结果见 Release 的 `bundle-verification.json`。

r9 将确认协议统一为 `confirm_owner`，正式微信绑定与验收角色统一为 `owner`；临时测试者保留独立验收范围。调用方须使用新字段。默认管理端口仍为 18700，EXE 和验证脚本均支持 `--port`。

r9 管理器回归 **33 项通过**，包含新增的三条 Muse 认证路径、管理台确认转发、正式与临时微信绑定及验收隔离。全部使用假凭据和独立本机测试数据。该结果为历史记录；本次分发资产以 r10 为准。

r8 的文字优化保留：明确完成的普通文字使用两次内容一致、至少 400ms 的就绪确认；图片、附件及完成状态不明的内容保留 3 秒等待。400ms 是确认窗口，不是手机收件延迟保证。

已有单组收发反馈；六组真实并行、媒体样本、MiMo 实际请求及授权过期恢复仍需账号使用者验收。[测试报告](packaging/TEST-REPORT.md) 与 [验收矩阵](packaging/ACCEPTANCE.md) 保留历史证据和待验项目。r10 补入可重复执行的转发、任务卡和发送门槛回归；原流程历史测试未全部迁入，不能把历史报告总数视为当前仓库的测试数量。开发测试还需可执行的 Node.js，见 [开发指南](CONTRIBUTING.md)。

2026-10-13 的[首组真实使用反馈](docs/USER-ACCEPTANCE-2026-10-13.md)已更新：使用者确认至少两轮文字往返正常，图片和文件也已测试。本机此前核对的运行目录为 r7 修订版，记录归入该运行实例；r10 发布包仍需对应版本的真实账号复验。

## 开发与重建

使用 CPython 3.13.14 Windows x64 完整开发环境，在仓库根目录执行：

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock.txt
New-Item -ItemType Directory -Path build-cache -Force
Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.13.14/python-3.13.14-embed-amd64.zip' -OutFile 'build-cache\python-3.13.14-embed-amd64.zip'
& .\.venv\Scripts\python.exe -I packaging\build.py --output 'C:\MuseBuild\MuseWeChatManager-0.1.0-preview-r11-review'
```

输出必须为不存在的新目录；构建验证 Python ZIP、生产依赖版本及上游源码哈希。完整步骤见操作手册。固定环境已用于本轮构建；另一台电脑上的全新联网安装仍需验证。

验证独立便携包时可以选择空闲管理端口：

```powershell
& .\.venv\Scripts\python.exe -I packaging\verify_bundle.py 'C:\MuseBuild\MuseWeChatManager-0.1.0-preview-r11-review.zip' 'C:\MuseBuild\r11-check' --port 19700
```

验证会改写这份临时解压副本的第一组测试端口，再启动、重启和停止其中的进程。源码测试与 EXE 验证应顺序运行；测试不进行真人登录或真实消息调用。

## 数据与许可

新建实例默认关闭正文日志，已有配置不自动改写。**助手发送队列会在本机 SQLite 保存明文正文**；日志若开启正文记录也会保存文本，保留到手动删除。DPAPI 只加密凭据，不加密全部聊天数据，内存传输的说明只针对凭据/附件负载。

登录后生成的 `secrets/`、`runtime/`、`manager-data/`、浏览器配置、聊天记录和日志属于本机数据，已设为 Git 忽略。DPAPI 文件即使加密也不应上传。示例配置无需 `.env`；实际密钥通过本机界面保存。

自有代码尚未授予 MIT、Apache-2.0 等开源许可证；即使仓库改为公开可见，也不因此新增开源授权。保留现有权利，后续授权方式由项目所有者另行决定。第三方组件保留原许可证，见 [第三方声明](THIRD-PARTY-NOTICES.md) 与 [权利说明](RIGHTS.md)。部分代码和文档由 ChatGPT/Codex 辅助生成和整理。

本项目不是 Muse、微信、MiMo 或 OpenAI 的官方产品。
