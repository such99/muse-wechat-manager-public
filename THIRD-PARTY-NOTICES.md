# 第三方组件与许可证

两个上游项目均为 MIT，许可证全文保留在 vendor/；来源、固定提交及文件哈希见 packaging/upstream-lock.json。

Python 3.13.14 的许可见 licenses/Python-LICENSE.txt。

下表记录固定生产依赖版本，沿用 r8/r9 的依赖基线。运行库完整文件随 GitHub Releases 的便携 ZIP 分发；源码仓库 distribution/ 只保留校验值。许可证和元数据另保存在 licenses/dependencies/ 供浏览；每个新发布包的实际组件与哈希以其 manifest.json 为准。

| 组件 | 版本 | 声明许可 |
| --- | --- | --- |
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| certifi | 2026.7.22 | MPL-2.0 |
| charset-normalizer | 3.5.2 | MIT |
| click | 8.5.0 | BSD-3-Clause |
| colorama | 0.4.6 | 见元数据 |
| fastapi | 0.143.0 | MIT |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| idna | 3.20 | BSD-3-Clause |
| opentelemetry-api | 1.45.1 | Apache-2.0 |
| pillow | 12.3.0 | MIT-CMU |
| psutil | 7.2.2 | BSD-3-Clause |
| pycryptodome | 3.24.0 | BSD, Public Domain |
| pydantic | 2.14.0 | MIT |
| pydantic_core | 2.50.0 | MIT |
| python-multipart | 0.0.32 | Apache-2.0 |
| qrcode | 8.2 | BSD |
| requests | 2.34.2 | Apache-2.0 |
| starlette | 1.7.0 | BSD-3-Clause |
| typing_extensions | 4.16.0 | PSF-2.0 |
| typing-inspection | 0.4.4 | MIT |
| urllib3 | 2.8.0 | MIT |
| uvicorn | 0.54.0 | BSD-3-Clause |
| websocket-client | 1.9.2 | Apache-2.0 |
| websockets | 17.2 | BSD-3-Clause |
