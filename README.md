# 欲求达与爬虫工具

欲求达桌面程序、内置平台、测试、开发脚本、打包配置及文档已统一放在 [欲求达](欲求达/README.md) 文件夹中。

近期改动见[更新日志](欲求达/文档/更新日志.md)。

已有环境可从仓库根目录启动：

```powershell
.\.venv\Scripts\python.exe .\欲求达\run_software.py
```

原命令 `python run_software.py` 仍可使用。开发和打包请先进入 `欲求达/`，参阅其中的说明。

已有 `data/`、`dist/` 和 `.venv/` 保留原位置；源码运行会继续读取现有 `data/software_app/`，避免丢失账号与下载历史。`YUQIUDA_HOME` 可显式指定运行数据目录。历史爬虫、参考工程和油猴脚本保留在原目录。

HentaiPaw 已支持作品预览、关键词搜索、阅读器原图下载和独立 Cookie 导入。E-Hentai / ExHentai、Google 相似图片和网页资源已标记为可用。能力范围与登录核查结果见 [平台说明](欲求达/文档/平台/试验模块与HentaiPaw.md)。
