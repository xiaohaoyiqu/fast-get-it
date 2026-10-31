# 浏览器登录与 Cookie

“欲求达”可以打开一个临时的可见 Chrome 窗口，让你直接在平台官网完成登录、验证码或双重验证。JMComic 使用单独的普通 Chrome 流程，避免验证页识别自动化启动参数。

整个过程不要求在软件里填写账号或密码，也不会保存密码。Cookie 等同登录凭证，只保存在 `data/software_app/`，不要提交到 Git 或发送给他人。

## 使用步骤

1. 打开 **设置 → 浏览器登录与 Cookie**。
2. 在平台下拉框选择 Twitter/X、Pixiv、FANBOX、Instagram、E-Hentai 表站或 ExHentai 里站。
3. 如需代理，先确认设置页上方的 **代理 URL**。登录窗口会直接使用输入框中的当前值，例如 `http://127.0.0.1:6987`。
4. 普通平台点击 **打开登录浏览器并获取**。EH 仅支持 **手动填写 EH Cookie** 或 **导入 Cookie 文件**。
5. 普通平台在弹出的官网窗口中自行登录并完成验证，不要把密码输入欲求达。EH 则在日常浏览器登录，从下述存储列表复制对应值。
6. 普通平台检测到必需 Cookie 后会保存会话、关闭临时窗口。EH 保存只检查本地格式，需联网检查时单独点击 **验证 EH 登录（联网）**。

登录等待上限为 10 分钟。需要提前结束时点击 **停止获取**；尚未检测到完整登录会话时不会覆盖现有 Cookie。

**EH 手动获取说明：** 打开 **F12 → Application（应用/应用程序）→ Storage（存储）→ Cookies**，选择表站 `e-hentai.org` 或里站 `exhentai.org`，读取名称和值，点“手动填写 EH Cookie”逐项粘贴保存；也可用“导入 Cookie 文件”读取 Cookie JSON／Netscape 文件。这是浏览器存储列表，不是 Console 或 Network；“请求头 TXT → JSON”只适用于实际 Cookie 请求头。EH 自动浏览器获取已移除。字段要求与详细步骤见 [EH 使用与登录](E-Hentai使用与登录.md#手动查看浏览器中保存的-cookie)。

登录窗口使用代理输入框中的当前值；Twitter 下载、在线资料、批量资料、关注刷新和头像缓存使用已保存的代理配置。修改代理后先点“保存设置”，再启动后续操作，使两者保持相同出口。Chrome 下载浏览器会收到显式代理参数；留空时保留系统代理行为。

如果平台拒绝自动化浏览器登录，可以在平时使用的 Chrome 或 Edge 完成登录，再使用 **请求头 TXT → JSON**。Twitter/X、Pixiv、FANBOX、Instagram、E-Hentai 表站和 ExHentai 里站都在通用登录卡片中提供该按钮；JMComic 在自己的登录卡片中提供同名按钮。把 Network 中同一个成功请求的完整请求标头复制到 TXT，即使后面同时附有响应头也可以：软件只提取 Cookie，先检查当前平台的必要字段，再在 TXT 旁生成不会覆盖旧文件的 Cookie JSON，并询问是否立即导入。

生成的 JSON 和原始 TXT 都含明文登录凭证，不要分享，用完应自行妥善删除。EH 导入保留原始 TXT 中可用的 User-Agent，保存不联网；JMComic 会从原始 TXT 同时保留允许列表内的安全请求头。导入后可使用连接检查确认会话。原有 **导入 Cookie 文件** 仍支持浏览器扩展导出的 JSON、Netscape `cookies.txt`、`Cookie: ...` 文本，以及名称和值分成相邻两行的格式。

“导入 Cookie 文件”的文件处理在后台运行，导入期间按钮暂时禁用，结果返回后恢复。EH 的“验证 EH 登录（联网）”也在后台单独运行，连接超时不会阻止本地保存。导入成功与站点会话有效是两项检查。

## 各平台保存规则

| 平台 | 完成标志 | 保存与后续用途 |
| --- | --- | --- |
| Twitter/X | `auth_token`、`ct0` | 保存为下载器使用的完整 Selenium Cookie 列表，用于主页、关注和帖子读取。X 经常拒绝 WebDriver 登录，推荐从普通浏览器导入。 |
| Pixiv | `PHPSESSID`，且账号接口验证成功 | 保存 Pixiv 会话，并保留已有 FANBOX Cookie；用于收藏、私密关注、R-18 和账号历史等登录功能。 |
| FANBOX | `FANBOXSESSID`，且支持方案接口验证成功 | 保存 FANBOX 会话，并保留已有 Pixiv Cookie；用于登录限定和已支持的付费帖子读取。 |
| Instagram | `sessionid` | 保存 Instagram 网页会话；用于公开页面被登录墙拦截或登录限定页面。 |
| E-Hentai 表站 | `ipb_member_id`、`ipb_pass_hash`；保存后可手动验证 | 手动填写或导入，保存为 `e-hentai-cookies.json`，只用于表站及表站收藏夹。 |
| ExHentai 里站 | `ipb_member_id`、`ipb_pass_hash`、`igneous`；保存后可手动验证 | 手动填写或导入，保存为 `exhentai-cookies.json`，只用于里站及里站收藏夹。 |

Pixiv 和 FANBOX 必须分别选择并登录一次，因为它们使用不同域名和会话 Cookie。第二次获取会合并文件，不会擦除第一次的登录资料。EH 表站与里站必须分别选择、分别手动保存；两个账号允许不同，Cookie 文件不会合并或互相复用。里站登录和后续访问应保持同一代理/VPN 出口。

JMComic 请使用设置页 **JMComic · 可用 → 站点与登录资料** 中的两个按钮：先点 **打开普通 Chrome 登录**，完成验证和登录；关闭这个专用 Chrome 的全部窗口，再点 **登录完成后获取**。登录阶段没有 WebDriver 和远程调试，专用资料启用了会话恢复；Windows 获取阶段直接读取同一资料目录，不会重新访问验证页或打开空白读取页。检测到当前域名的非空 `AVS` 才会更新正式 Cookie；失败时保留原文件，`cf_clearance` 本身不能证明账号已登录。也可点 **请求头 TXT → JSON** 导入浏览器抓包；转换出的 JSON 只含 Cookie，正式导入仍从原 TXT 读取匹配的安全导航请求头，避免 Cloudflare 环境信息丢失。

## 不使用网页 Cookie 的平台

- **Bluesky**：公开资料使用公共 AppView API，不需要 Cookie。读取自己账号的拉黑或静音名单时，继续在黑名单管理中临时填写 handle 和应用密码；应用密码不保存。
- **Google Lens 相似搜索**：只打开搜索验证窗口，不需要也不会保存 Google 账号 Cookie。
- **普通网页下载**：当前按用户明确选择的页面读取公开资源，没有通用账号 Cookie 仓库，避免把一个站点的登录凭证错误发送给另一个站点。

## 与 instagram-cookie-generator 的关系

参考项目 `vovinacci/instagram-cookie-generator` 使用 Selenium 登录 Instagram，并定时把 Cookie 写成 Netscape 文件。欲求达采用相同的“浏览器完成登录后读取会话”思路，但做了以下调整：

- 使用可见浏览器，让用户自行处理验证码、双重验证和挑战页面。
- 不在 `.env` 或软件设置中保存 Instagram 用户名和密码。
- 使用项目已有的 ChromeDriver 和代理设置。
- 根据各平台现有适配器需要，分别保存 Selenium 列表或名称到值的 JSON。
- 使用临时浏览器资料目录，任务结束后清理；Cookie 文件仍保留在项目运行数据目录。

参考项目已经归档，可用于理解流程，不建议直接用它保存明文账号密码或替换本功能。

## 常见问题

### 已经登录，但窗口没有自动关闭

确认登录后实际停留在对应平台官网。软件会继续验证账号接口，避免把网站在登录前发放的访客 Cookie 误当成有效登录。Pixiv 登录只会完成 Pixiv 获取；FANBOX 需要另选 FANBOX 再登录。Twitter 显示拒绝自动化登录时，关闭该窗口并改用普通浏览器文件导入。JMComic 不在这个通用入口中；按上方普通 Chrome 两阶段步骤操作。

### 网站反复要求验证或返回 403

浏览器登录和软件后续请求应使用相同代理出口。JMComic 还需要相同站点域名和刚捕获的 User-Agent。代理发生变化、Cookie 过期或站点切换域名后，应重新获取。

Twitter 的媒体请求遇到 401/403 不再逐次盲目重试，应先检查 Cookie、账号权限和出口；429、临时服务错误和网络异常仍可能进入有限重试。这个分类仅描述当前 Twitter 请求链路，其他平台按各自实现处理。

### ChromeDriver 启动失败

先点击设置页底部的 **检测浏览器驱动**；缺少或不匹配时点 **安装 / 更新驱动**。还应确认 Google Chrome 可用，并且没有安全软件阻止临时浏览器资料目录。源码版全局驱动通常位于 `data/software_app/browser/`；单文件 exe 可以使用临时解压的内置驱动，也会优先识别 exe 同级驱动，实际位置以检测结果为准。
