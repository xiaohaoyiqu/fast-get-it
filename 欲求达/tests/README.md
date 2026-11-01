# 测试与验收入口

自动回归位于 `tests/test_*.py`，可复现的原生界面、浏览器、媒体和实站验收位于 `tests/manual/`。两类源码均保留；生成的构建、样本和缓存可以清理，结果报告与测试源码用途不同。

## 自动回归与静态检查

在项目根目录运行，使用已安装 `requirements-dev.txt` 的开发环境：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests
.\.venv\Scripts\python.exe -X utf8 scripts/check_quality.py
.\.venv\Scripts\python.exe -X utf8 scripts/check_documentation.py
```

自动回归主要使用生成样本、临时目录和本机服务，不代表真实平台账号功能已通过。Windows 临时目录及用户输出锁可能受执行环境权限限制，需区分环境错误和产品故障。运行时生成的验收证据保存在开发环境中。

## 手动验收

| 入口 | 用途与条件 |
| --- | --- |
| `verify_layout_smoke.py` | 主界面按钮可达性、不同尺寸与字体缩放；使用隔离数据 |
| `verify_daily_acceptance.py` | 编排 16 组本机验收，可通过 `--only` 选择组；会启动真实窗口、浏览器和本机工具 |
| `verify_desktop_smoke.py`、`verify_person_config_smoke.py`、`verify_eh_manual_smoke.py` | 桌面、人物配置和 EH 表单 |
| `verify_transfer_cache_smoke.py`、`verify_library_integrity_smoke.py` | 传输缓存维护和成品清单核验 |
| `verify_runtime_smoke.py browser`、`verify_browser_failure_smoke.py`、`verify_browser_orphan_smoke.py` | 实际 Chrome、关窗／崩溃与进程回收；需要浏览器及相应驱动 |
| `verify_http_checkpoint_smoke.py`、`verify_http_redirect_resume_smoke.py` | 本机 HTTP、强退检查点与重定向续传 |
| `verify_bandwidth_smoke.py`、`verify_domain_backoff_smoke.py` | 本机 HTTP 共享限速和长退避 |
| `verify_aria2_limits_smoke.py`、`verify_hls_resume_smoke.py`、`verify_media_engine_smoke.py` | aria2、HLS 及真实 FFmpeg；相关外部工具需可用 |
| `verify_tool_install_smoke.py` | 生成候选工具并验证安装失败保护 |
| `verify_package_smoke.py` | 检查指定目录的 GUI／CLI 成品；`--gui` 会打开窗口并正常关闭 |
| `verify_daily_platform_live.py` | 按平台执行有界实站验收；访问网站，部分平台读取指定运行目录中已保存的登录资料 |
| `verify_pixiv_live_smoke.py`、`verify_eh_live_smoke.py` | 平台专项实站入口，先阅读参数与脚本范围再运行 |
| `verify_hentaipaw_live_smoke.py` | HentaiPaw 详情与原图列表、可选搜索及单页下载校验；默认匿名，账号测试需显式提供 Cookie 文件 |

本机编排示例：

```powershell
.\.venv\Scripts\python.exe -X utf8 tests/manual/verify_daily_acceptance.py --root data/software_app/verification/local-check
.\.venv\Scripts\python.exe -X utf8 tests/manual/verify_daily_acceptance.py --root data/software_app/verification/local-check --only http_checkpoint http_redirect
.\.venv\Scripts\python.exe -X utf8 tests/manual/verify_package_smoke.py dist
```

`verify_runtime_smoke.py database` 是数据库迁移验收入口，会备份并在验证中访问当前用户数据库，不属于日常默认烟测。不要因为整理文档而运行它。实站脚本也不包含在本机日常编排中；Pixiv 账号继续按用户要求跳过。

## 产物保留规则

- 可清理：`__pycache__/`、Ruff 缓存、打包中间目录、重复工具安装、已完成验收的生成样本及过期测试 EXE。
- 保留：最终及失败报告、必要日志、代表性截图、当前独立验收包和一个回退包。
- 用户 Cookie、长期浏览器资料、运行数据库、真实下载目录、固定输出锁及参考项目不按测试垃圾处理。

测试源码是否仍有用，应按覆盖的行为和调用关系判断，不能因为文件名包含 test 或 manual 就删除。
