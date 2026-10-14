# 开发与贡献

项目目前为公开预览阶段。开发基线为 Windows x64、CPython 3.13.14、Google Chrome 与系统 .NET Framework 4.x；安装及构建命令见 README 和 packaging/USER-GUIDE。

## 修改范围

- 应用服务与界面位于 `real/`，构建与启动器位于 `packaging/`。
- `vendor/` 保存选定上游源码，内容受 `packaging/upstream-lock.json` 校验。修改时应说明来源与差异，审查后更新哈希；不要绕过校验。
- 启动器与交付文档只维护 `packaging/` 中的版本；便携包根目录的副本由构建脚本生成。
- 新发布包使用全新输出目录构建，不从已经登录或聊天的安装目录打包。
- ZIP、EXE、manifest 和便携包验证结果上传到 GitHub Releases；仓库 `distribution/` 只提交对应的 `SHA256SUMS.txt`。不要将二进制重新加入 Git 历史。

## 验证

在已安装固定开发依赖的 Windows 开发环境中，另需安装 Node.js（测试使用内置模块，无需 npm install）；可通过 `NODE_BINARY` 指定其可执行文件，否则从 PATH 查找。运行以下回归：

```powershell
New-Item -ItemType Directory -Path .pytest-work -Force
& .\.venv\Scripts\python.exe -I -m pytest real/test_feed.py real/test_feed_task_filter.py real/test_unlimited_forwarding.py real/test_manager.py --basetemp .pytest-work\regression
```

`--basetemp` 必须使用专门的可丢弃测试目录，pytest 会清理其中内容；不得指向任何工作数据目录。源码测试和 EXE 验证应顺序运行，避免端口竞争。EXE 验证脚本支持 `--port 19700` 等空闲管理端口，且只在独立解压副本中分配第一组测试端口；不要指向正在使用的目录。

本次随源码和便携包提供 91 项测试：39 项转发、10 项卡片过滤、9 项发送策略、33 项管理器。其中两个测试调用 Node，含 33 组核心与 39 组过滤检查。更早的原工程历史测试未全部迁入，不计入当前 91 项。Windows CI 尚未配置；本轮结果来自独立本机测试。

## 问题报告与改动说明

提供版本号、Windows/Python/Chrome 版本、最小复现步骤、期望和实际结果，以及脱敏后的错误信息。说明是否涉及文字、图片、附件、授权恢复或多实例；不要附带真实聊天正文、账号标识、Cookie、Token、二维码授权链接、API Key、DPAPI 文件或浏览器配置。

提交改动时说明行为变化、验证方法和已知限制。模拟测试、用户反馈与受控真实账号验收分别记录。真实消息和付费 API 调用由账号使用者自行控制，不纳入无人值守测试。

安全问题请按 [SECURITY.md](SECURITY.md) 私下报告，不要通过公开问题或讨论披露可利用细节和凭据。
