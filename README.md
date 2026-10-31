# 欲求达

欲求达是一个 Windows 桌面下载工具，用于查找、预览和保存多个平台的图片、视频、音频及作品信息。

支持任务队列、暂停与恢复、失败重试、媒体补下、下载库、文件核验和跨平台黑名单。

## 支持的平台

| 平台 | 已接入能力 |
| --- | --- |
| Twitter / X | 作者、单帖、关注列表、图片与视频、下载历史 |
| Pixiv | 作品、作者、系列、小说、排行、关注、收藏、FANBOX、ugoira |
| JMComic | 搜索、漫画与章节、收藏、观看记录、PDF 与长图 |
| Bluesky | 用户、公开关注、帖子搜索、图片和 HLS 视频 |
| Instagram | 账号、帖子和 Reels 的预览与媒体入口 |
| E-Hentai / ExHentai | 独立登录、搜索、收藏、展示图及可选种子下载 |
| Google 相似图片 | 单图或文件夹查询、候选核对与网页下载转交 |
| 普通网页 | 静态资源提取与浏览器页面媒体发现 |

平台页面、登录状态和访问限制会影响可用性。Instagram 帖子／Reels、Google 人机验证及 JM 会话等场景仍有实际使用限制，不能把接口接入视为所有业务均已验证。

## 启动

使用 64 位 Python 3.10 或更高版本，在项目目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-app.txt
.\.venv\Scripts\python.exe run_software.py
```

已有打包文件时，也可以直接运行 `dist/欲求达.exe`。EXE、账号资料和下载内容不包含在源码仓库中。

## 界面使用

- 左侧查找目标、查看作者资料和管理黑名单；资料卡可滚动查看完整内容。
- 顶部选择平台、填写目标和保存目录，使用“开始任务”“本地预览”或“在线获取资料”。
- “更多操作”提供混合平台批量任务、人物配置、单次设置下载、执行前检查和输入说明。
- 任务队列支持暂停、恢复、重试和媒体补下；较小窗口中可滚动操作区，任务日志保留独立显示区域。
- 下载库支持搜索、筛选、导出、清理以及成品清单核验。
- 设置页支持纵向和横向滚动，“保存设置”固定在页面底部。

完整步骤见 [用户手册](文档/用户手册.md)。

## 登录与数据

通过设置页填写或导入所需登录资料。E-Hentai 与 ExHentai 分别保存 Cookie；保存和联网验证为独立操作。

程序数据保存在 `data/software_app/`，下载目录可在界面修改。更新程序时保留数据目录。Cookie、数据库、浏览器资料和下载记录不应公开上传。

## 打包

打包前准备根目录 `1.ico` 和匹配的 `software_app/crawlers/twitter/chromedriver.exe`。驱动不会随源码提供，可从设置页安装后复制到打包路径。

```powershell
.\.venv\Scripts\python.exe -m PyInstaller 欲求达.spec --clean --noconfirm
```

输出为 `dist/欲求达.exe` 和 `dist/欲求达-cli.exe`。替换旧文件前关闭对应程序，保留账号数据和下载目录。

## 开发检查

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe scripts/check_quality.py
.\.venv\Scripts\python.exe scripts/check_documentation.py
```

原生界面与手动验收入口见 [测试说明](tests/README.md)，实现约定见 [开发说明](文档/开发与架构说明.md)。

## 文档

- [用户手册](文档/用户手册.md)
- [平台与登录说明](文档/README.md)
- [插件开发](文档/插件开发.md)
- [鸣谢](文档/鸣谢.md)

配套浏览器脚本独立维护：[tempermonkey-scripts](https://github.com/xiaohaoyiqu/tempermonkey-scripts)。

下载功能用于保存自己有权访问的内容。遇到平台结构或接口变化，请在本仓库反馈问题。
