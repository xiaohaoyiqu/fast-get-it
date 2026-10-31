# Bluesky 与 Instagram 使用说明

本文说明“欲求达”桌面端和项目内两个油猴脚本当前可用的功能、配置方法与限制。

## 安装和启动

建议明确使用实际启动软件的 Python 版本安装依赖：

```powershell
py -3.12 -m pip install -r requirements-app.txt
py -3.12 .\run_software.py
```

如果启动显示 `No module named 'Crypto'`，执行：

```powershell
py -3.12 -m pip install "pycryptodome>=3.20"
```

`Crypto` 是 PyCryptodome 提供的导入名。电脑上有多个 Python 时，安装命令和启动命令必须使用同一个版本。

## Bluesky

### 桌面端可用功能

| 搜索种类 | 输入 | 结果 |
| --- | --- | --- |
| 关注账号 | `name.bsky.social`、自定义域名 handle 或 DID | 读取该账号的公开关注列表 |
| 用户搜索 | 昵称、handle 或关键词 | 返回公开用户候选 |
| 帖子搜索 | 关键词 | 返回公开帖子候选；部分代理出口可能被全文搜索接口拒绝 |
| 帖子 / 媒体 | 具体 `bsky.app/profile/.../post/...` 链接 | 预览并下载该帖媒体 |

账号主页也可以直接作为任务目标。作者主页任务读取带媒体的公开动态；类型 `1` 下载图片，类型 `2` 下载视频。图片使用公开全尺寸资源，视频 HLS 通过 FFmpeg 保存为 MP4。

公开资料、公开关注、作者动态和具体帖子使用 Bluesky 公共 AppView，不要求登录。读取自己账号的拉黑/静音名单仍需在 **管理黑名单 → Bluesky 账号** 中临时输入 handle 和应用密码；应用密码不保存。

### HLS 点播恢复

已结束的未加密 HLS 支持完整分片恢复，包括 TS 和带初始化数据的 fMP4。主列表选择当前最高带宽变体，HTTP 下载完成后再由 FFmpeg 读取本地列表封装；直播、加密、字节范围、低延迟增量布局及独立音轨／视频轨暂不支持。

失败或取消后沿用同一输出位置重试：软件重新读取列表，核对分片大小／SHA-256，再使用 ETag 或 Last-Modified 条件请求，仅匹配的 304 才复用。没有验证器时重下；列表、地址签名或资源内容变化时重新建立缓存，未完成的单片也从头获取。缓存位于 `.yuqiuda-hls-<摘要>/`，成功后清理自己的文件。视频依然是逐媒体清单中的一项，内部资源进度和条件复用数写入任务日志。

分片和初始化数据使用已保存代理，参与本实例媒体 HTTP 总限速和主机退避；FFmpeg 只做本地封装。旧 `.part.mp4` 不自动接管，数据库结构与账号配置不变。当前限制和清理规则见 [用户手册](../用户手册.md#bluesky-hls-分片恢复)。

## Instagram

### 桌面端可用功能

Instagram 当前适配器标记为可用（`ready`），支持以下精确目标：

- 用户名、`@用户名` 或账号主页。
- `/p/短代码/` 帖子链接。
- `/reel/短代码/` Reels 链接。
- 旧 `/tv/短代码/` 视频链接。

本地预览只识别目标类型。在线获取资料会读取页面的 Open Graph、JSON-LD 和嵌入 JSON；开始任务后下载其中可验证为 Instagram CDN 的图片或视频。类型 `1` 为图片，类型 `2` 为视频。

公开页面也可能被 Instagram 改成登录页或挑战页。此时需要导入浏览器中当前登录会话的 Cookie：

1. 推荐在 **设置 → 浏览器登录与 Cookie** 选择 Instagram，点击 **打开登录浏览器并获取**。
2. 在弹出的 Instagram 官网窗口中登录并完成验证；软件检测到 `sessionid` 后自动保存，账号密码不会进入软件。
3. 已有导出文件时，也可以在 **Instagram 候选 → 导入 Cookie** 选择 JSON、Netscape Cookie 文件或完整的 `Cookie` 请求头文本。
4. 状态出现 `sessionid` 已就绪后，再执行在线预览或下载。

Cookie 保存到 `data/software_app/instagram/cookies.json`，不会写入任务日志。不要把该文件提交到 Git 或发送给他人。

通用登录窗口的完整步骤及与 `instagram-cookie-generator` 的差异见 [浏览器登录与 Cookie](浏览器登录与Cookie.md)。

当前未接入普通账号的关注列表；对应选项显示“需登录，规划”并禁止创建任务。Meta 官方 Instagram API 主要服务 Business/Creator 专业账号，不能用来替代普通个人账号的网页登录会话。

## 逐媒体结果与补下

Bluesky 和 Instagram 静态下载已接入 SQLite 逐媒体结果，任务可显示“部分完成”，详情可导出失败清单或仅补未完成媒体。补下会刷新原页面并核对原序号、类型和 URL 路径摘要，沿用原来的子目录；媒体变化时保留失败，不直接下载同序号的其他内容。原始签名媒体 URL 不写入这份清单。

Instagram 普通帖子链接统一使用 `/p/<id>/`。Instagram 浏览器渲染回退尚未接入逐媒体结果；静态媒体传输失败不再触发整页浏览器回退。原页面不再返回已记录媒体时，需要人工检查。

## 油猴脚本

浏览器脚本单独维护在 [xiaohaoyiqu/tempermonkey-scripts](https://github.com/xiaohaoyiqu/tempermonkey-scripts)。当前项目中的 `油猴脚本/` 是该仓库的本地工作副本，不随欲求达主仓库提交。

当前推荐脚本位于：

- `油猴脚本/bluesky-media-downloader.user.js`
- `油猴脚本/instagram-media-downloader.user.js`

脚本适合在已经打开、已经登录的网页中直接处理当前可见内容：

- Bluesky 脚本通过公共 API 补全帖子媒体，提供页面下载入口、临时队列、ZIP、视频转 GIF 和抽取音频。
- Instagram 脚本读取当前登录页面的图片、视频、JSON-LD 和页面数据，支持帖子/Reels 下载及临时队列。

桌面端复用了两份脚本验证过的目标识别和媒体提取思路，并把结果接入统一任务队列、下载库、历史、代理与黑名单。浏览器脚本不会自动把临时队列同步到桌面端。

安装油猴脚本时需要 Tampermonkey 或兼容管理器。脚本中的 `@match` 决定生效页面，跨域请求需同时声明 `@grant GM_xmlhttpRequest` 和相应 `@connect` 域名。

## 黑名单

- Bluesky 主页可保存 handle 或 DID 作者规则，帖子链接可保存作品规则。
- Instagram 主页可保存用户名作者规则，帖子、Reels 和 TV 链接可按短代码保存作品规则。
- 相似图片候选命中这些链接时会显示屏蔽状态，批量下载会跳过。
- 本地黑名单只控制“欲求达”的候选和下载，不会向 Bluesky 或 Instagram 写入拉黑操作。

## 已知限制

- Bluesky 帖子全文搜索可能拒绝某些代理出口；界面会保留其他公开功能并显示明确原因。
- Bluesky HLS 视频依赖可找到的 FFmpeg；当前查找顺序为 `YUQIUDA_FFMPEG`、PATH、程序资源根下 `tools/ffmpeg*.exe`。不能仅凭 exe 为单文件包就假定已包含该工具；发行包应单独验证视频下载。源码模式可在 PATH 中提供 `ffmpeg`，或用 `YUQIUDA_FFMPEG` 指定文件。
- Instagram 页面结构和登录验证会变化；遇到登录页、挑战页或无媒体页面时不会伪装成下载成功。
- Instagram 账号主页可能只返回资料而不返回帖子媒体。下载时优先使用具体帖子或 Reels 链接。
