# 欲求达

欲求达是一个 Windows 桌面下载工具，用来查找、预览和保存多个内容平台上的图片、视频、音频及作品元数据。桌面端把平台候选、任务队列、下载历史、文件预览和黑名单放在同一个界面中；账号密码始终留在网站登录页，软件只读取后续请求需要的 Cookie。

项目源码放在 [xiaohaoyiqu/fast-get-it](https://github.com/xiaohaoyiqu/fast-get-it)。配套浏览器脚本放在 [xiaohaoyiqu/tempermonkey-scripts](https://github.com/xiaohaoyiqu/tempermonkey-scripts)。

## 目前能做什么

| 平台 | 状态 | 已接入内容 |
| --- | --- | --- |
| Twitter/X | 可用 | 用户、单帖、关注列表、媒体下载、历史与黑名单 |
| Pixiv | 可用 | 作品、作者、系列、小说、排行、关注、收藏、FANBOX 与 ugoira |
| JMComic | 可用 | 搜索、详情、章节、收藏、观看记录、PDF 与长图后处理 |
| Bluesky | 可用 | 用户、关注、帖子搜索、单帖图片和 HLS 视频 |
| Instagram | 可用 | 账号、帖子与 Reels 预览和媒体下载；普通账号关注列表尚未接入 |
| E-Hentai / ExHentai | 测试中 | 表里站独立登录、搜索、收藏、正常展示图、元数据和可选种子下载 |
| Google 相似图片 | 测试中 | 单图或文件夹查询、候选检查、导出，并转交网页资源下载器 |
| 普通网页 | 测试中 | 静态资源提取；复杂的动态页面仍可能需要人工打开 |

“可用”表示主要任务已经接入桌面端并经过回归测试。“测试中”表示流程能运行，但网站验证、页面变化或账号条件仍可能影响结果。

## 从源码启动

建议使用 64 位 Python 3.10 或更高版本。

```powershell
git clone https://github.com/xiaohaoyiqu/fast-get-it.git
cd fast-get-it
python -m pip install -r .\requirements-app.txt
python .\run_software.py
```

首次启动后打开“设置”，先确认下载目录、代理和 ChromeDriver。点击“安装 / 更新驱动”时，软件会读取本机 Chrome 版本并自动选择对应驱动，不需要手动下载版本号。

常用诊断命令如下。

| 命令 | 用途 |
| --- | --- |
| `python .\run_software.py modules` | 列出平台模块及缺少的配置 |
| `python .\run_software.py browser-driver` | 检查 Chrome 与 ChromeDriver 版本 |
| `python .\run_software.py browser-driver --download` | 自动安装或更新匹配的 ChromeDriver |
| `python .\run_software.py aria2` | 检查可选的 aria2 BT 引擎 |
| `python -m unittest discover -s tests -p "test_*.py"` | 运行离线回归测试 |

## 登录资料和本地数据

程序运行数据保存在 `data/software_app/`。Windows 安装版改用 `%LOCALAPPDATA%\YuqiuDa\data\software_app`，默认下载到用户的 `Downloads\欲求达`。

Twitter/X、Pixiv、FANBOX 和 Instagram 可以在设置页打开临时登录浏览器。JMComic 与 E-Hentai 使用普通 Chrome 登录资料，关闭登录窗口后再读取 Cookie。E-Hentai 表站和 ExHentai 里站分别保存会话，两个账号不会互相覆盖。

Cookie、请求头、SQLite、浏览器资料和下载记录都属于本机数据。`.gitignore` 会排除整个 `data/software_app/`，但仍不应把这些文件复制到问题反馈或聊天记录中。

## E-Hentai 种子下载

保存 `.torrent` 与使用 aria2 下载内容是两个独立开关，默认都关闭。aria2 安装包会经过固定 SHA-256 校验，任务完成后停止做种；当前命令同时关闭 DHT、IPv6 DHT、PEX 和本地节点发现。

BT 下载期间，Tracker 和其他节点仍可能看到公网 IP，也可能产生上传流量。请确认网络环境和内容使用权限后再开启。软件不会自动购买可能消耗 GP 或 Credits 的官方归档。

## Windows 打包

```powershell
python -m pip install -r .\requirements-build.txt
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows.ps1
```

构建结果位于 `dist\欲求达\`。`欲求达.exe` 是桌面程序，`欲求达-cli.exe` 用于诊断。两者使用根目录 `蔑视.png` 生成的多尺寸 Windows 图标。

生成安装程序需要 Inno Setup 6，具体命令和数据目录见 [Windows 打包与安装](文档/Windows打包与安装.md)。

## 目录

| 路径 | 内容 |
| --- | --- |
| `software_app/` | 当前桌面程序、平台适配器、下载器和公共服务 |
| `tests/` | 离线回归测试 |
| `scripts/` | 构建与可重复烟测脚本 |
| `packaging/` | Windows 图标、运行钩子和 Inno Setup 配置 |
| `文档/` | 用户手册、平台说明、架构和测试记录 |
| `data/software_app/` | 本机运行数据，不进入版本库 |
| `油猴脚本/` | 独立仓库的本地工作副本，不随主仓库提交 |

根目录的 `推特爬虫/`、`Pixiv爬虫/`、`JMComic爬虫/` 和 `谷歌图片搜索工具/` 是旧实现与行为参考。当前程序不从这些目录启动。

## 文档入口

- [用户手册](文档/用户手册.md)
- [浏览器登录与 Cookie](文档/平台/浏览器登录与Cookie.md)
- [E-Hentai / ExHentai](文档/平台/E-Hentai使用与登录.md)
- [开发与架构说明](文档/开发与架构说明.md)
- [仓库与目录说明](文档/仓库与目录说明.md)
- [完整文档目录](文档/README.md)

下载功能只应用于自己有权访问和保存的内容。平台规则或页面结构变化后，相关模块可能需要更新。
