# 平台状态与 HentaiPaw

核查日期：2026-11-01。`ready` 表示已接入常规功能，`alpha` 在界面显示“试验”，`planned` 表示规划或接入准备。自动回归验证本地行为，不能代替真实平台登录、搜索和下载验收。

## 已调整为可用的模块

| 模块 | 当前已实现 | 使用范围与条件 |
| --- | --- | --- |
| E-Hentai / ExHentai | 独立 Cookie、画廊预览、搜索、收藏读取与单项管理、展示图和元数据、可选种子 | 里站访问依赖账号和网络；不自动购买官方 ZIP 归档，种子只保存 |
| Google 相似图片 | 本地图片/目录搜索、Lens 候选页发现、候选去重及网页资源转交 | 验证页、失效页面和 Lens 页面变化仍影响搜索；AI 概览不能当作匹配结果 |
| 网页资源 | 静态媒体提取、浏览器发现、公开地址与重定向检查、大小限制及取消 | 网站结构差异较大，页面可见媒体并不保证能下载完整作品 |

以上三个模块按用户要求由 `alpha` 调整为 `ready`，界面显示“可用”。相关测试包括 `test_ehentai*.py`、`test_native_crawlers.py`、`test_webpage_crawler.py`、`test_link_availability.py` 和浏览器、Cookie、传输专项测试。可用标记不改变上表的账号及网络条件。

## HentaiPaw 登录核查

通过系统代理匿名读取首页、作品详情和阅读器，均返回 HTTP 200；`/login` 返回 HTTP 404。公开页面及已读取的公开脚本未发现可确认的账号登录接口或必要会话 Cookie 名称。用户在网页下载操作中看到的登录提示，是否属于前端限制仍未确认，不能据此认定全站不需要登录。

使用实际客户端匿名下载一页，得到有效 WebP 图片（1360 × 2040，231002 字节）。该样本阅读器包含 202 页原图，关键词搜索也返回候选。本次没有使用真实账号 Cookie，也没有验证账号限定内容。

已确认的页面结构：

- 中文首页：`https://zh.hentaipaw.com/`。
- 详情页：`/articles/数字ID`。
- 搜索表单：`/articles/search`，关键词参数 `keyword`。
- 阅读器：`/viewer?articleId=数字ID&page=页码`。
- 详情页图片路径包含 `/thumbnails/`，封面及分页缩略图不等于阅读原图。

## Cookie 导入

设置 → 浏览器登录与 Cookie → HentaiPaw → 导入 Cookie 文件。支持 JSON、Netscape `cookies.txt` 和 `Cookie:` 请求头文本，单独保存在 `data/software_app/hentaipaw/cookies.json`。保留 Cookie 的域、路径、HTTPS 和过期时间；拒绝其他平台域的 Cookie。请求头 TXT 也可先转换成 JSON 再导入。

当前使用导入方式，不提供未经核实的自动账号登录流程。导入成功只表示配置已保存，不显示“账号登录已验证”；公开页读取成功也不能证明账号已登录。未配置 Cookie 时尝试公开访问；遇到 HTTP 401/403 或阅读器跳转时，会提示检查登录、站点验证及代理。原图 CDN 使用匿名会话，不接收站点 Cookie。

## 预览、搜索与下载

`hentaipaw` 模块标记为可用，支持数字 ID、HTTPS 详情链接、本地识别、在线作品预览、关键词搜索和整部作品的阅读器原图下载。搜索当前读取首个结果页，可选候选加入队列。混合链接批次自动识别 HentaiPaw，数字 ID 与链接共享作品黑名单标识。

原图列表来自阅读器的 Next.js 数据，按 JSON 和字节长度解析，不执行页面脚本。校验作品 ID、CDN 域名、原图路径及连续页码，拒绝以缩略图代替原图。下载复用公共传输引擎，支持重试、可验证的断点续传、复用已有文件、取消、图片校验及输出格式转换。失败页会报告错误，可重试补下；同时保存作品元数据并把结果写入下载库。

请求复用网页模块的公开目标检查、重定向检查、超时、取消和 5 MB HTML 上限。在线预览返回的可见页链接数不承诺等于总页数，下载采用阅读器返回的完整原图列表。

```powershell
python run_software.py preview hentaipaw 123
python run_software.py preview hentaipaw https://zh.hentaipaw.com/articles/123 --live
python run_software.py search hentaipaw "关键词"
python run_software.py download hentaipaw https://zh.hentaipaw.com/articles/123 --output-dir D:\Downloads
```

示例 ID 仅用于演示语法，请替换为真实作品链接。桌面中选择 HentaiPaw，可以获取在线资料、搜索候选或开始下载；需要代理时请在软件设置中保存代理地址。

## 验收入口

自动回归：`tests/test_hentaipaw.py`，覆盖原图解析、拆分数据帧、字节长度文本、缩略图拒绝、作品和来源校验、登录错误、Cookie 范围、搜索、批次、黑名单、下载限量及取消。

手动实站入口：`tests/manual/verify_hentaipaw_live_smoke.py`。传入真实作品链接与 `--root`，默认匿名只读检查；`--download-one` 最多下载一页并验证图片，`--search` 可检查搜索，`--proxy` 可指定代理，`--cookie-file` 显式指定测试 Cookie。默认不读取用户已保存账号。
