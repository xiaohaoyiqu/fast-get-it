# twitter_Crawler

> 本文保留内置爬虫脚本的独立用法。欲求达桌面端从项目根目录运行 `python .\run_software.py`，经 `service.py` 在应用进程内调用爬虫，运行资料位于 `data/software_app/twitter/`。桌面设置、批量队列、命名与未完成清单以 [用户手册](../../../文档/用户手册.md) 为准；下方独立脚本默认值不等同桌面任务默认值。

用于下载 X/Twitter 指定用户媒体资源的脚本。当前主入口是 `twitter_Crawler_2.py`，支持：

- 从 `X_cookie.json` 注入登录 cookie，不再用 Selenium 自动登录 X。
- 单用户或批量下载，最多 5 个目标。
- 支持可选多用户并行爬取，默认串行。
- 每个目标单独选择下载类型。
- 支持图片、视频、GIF 动图、从视频单独提取音频。
- GIF 动图默认转换为 `.gif`，可用 `--no-convert-gif` 保留 X 返回的 MP4。
- 下载记录指向的媒体文件仍存在时跳过相同 URL；文件已删除时允许重新下载。
- 从 X 的网络 JSON 响应中提取媒体，尽量覆盖同一条推文里的多张图片。
- 下载失败的 URL 会写入失败清单，后续成功下载会自动移除。
- 更稳定的文件命名和下载统计输出。

## 快速开始

先在项目根目录安装 `requirements-app.txt` 中的依赖，再进入本目录执行下方独立脚本命令：

```powershell
Set-Location .\software_app\crawlers\twitter
```

如需让 Cookie、配置和记录与桌面端统一管理，请使用项目根目录的 `run_software.py` 桌面入口。独立脚本以当前工作目录解析默认相对路径；不要把测试或示例生成的 Cookie、记录加入 Git。

先准备 cookie：

```powershell
python .\set_cookie.py --guide
```

按提示从普通浏览器已登录的 `x.com` 复制 cookie，然后生成 `X_cookie.json`。

然后运行爬虫：

```powershell
python .\twitter_Crawler_2.py
```

如果 `config.json` 里没有目标，程序会让你交互输入目标和下载类型。

## 查看帮助

两个主要脚本都支持 `--help`：

```powershell
python .\set_cookie.py --help
python .\twitter_Crawler_2.py --help
```

`set_cookie.py --help` 会显示 cookie 获取方式。

`twitter_Crawler_2.py --help` 会显示命令行参数、下载类型、批量下载和配置示例。

## 获取 Cookie

现在不建议再用 Selenium 打开登录页获取 cookie，因为 X 容易提示“已临时限制你的登录”。推荐从普通浏览器中复制。

### 方式一：复制整段 Cookie 请求头

1. 用平时使用的 Chrome 或 Edge 打开 `https://x.com/`，确认账号已登录。
2. 按 `F12` 打开开发者工具，切到 `Network / 网络`。
3. 刷新 x.com 页面。
4. 点任意一个 x.com 请求，例如 `HomeTimeline`、`UserTweets`、`UserMedia`、`graphql` 或主文档。
5. 在 `Headers / 标头` 中找到 `Request Headers / 请求标头`。
6. 复制 `Cookie:` 这一行冒号后面的整段内容。
7. 运行：

```powershell
python .\set_cookie.py
```

把整段 Cookie 粘贴进去即可。

### 方式二：手动复制 auth_token 和 ct0

1. 用普通浏览器登录 `https://x.com/`。
2. 按 `F12`，切到 `Application / 应用`。
3. 左侧进入 `Storage / 存储 -> Cookies -> https://x.com`。
4. 分别找到 `auth_token` 和 `ct0`，复制 `Value / 值`。
5. 运行：

```powershell
python .\set_cookie.py
```

不粘贴整段 Cookie，直接回车，然后按提示输入两个值。

### 方式三：导入 cookie 文件

支持浏览器插件导出的 JSON 或 `cookies.txt / Netscape` 格式：

```powershell
python .\set_cookie.py D:\path\cookies.json
python .\set_cookie.py D:\path\cookies.txt
```

生成的新 cookie 会保存为 `X_cookie.json`。如果旧文件存在，脚本会先备份。

## 下载类型

`types` 用数字组合表示：

| 值 | 含义 |
| --- | --- |
| `1` | 图片 |
| `2` | 视频 |
| `3` | GIF 动图，默认转换 GIF，可选择保留 MP4 |
| `4` | 从视频单独提取音频 |
| `12` | 图片 + 视频 |
| `13` | 图片 + GIF |
| `23` | 视频 + GIF |
| `123` | 图片 + 视频 + GIF |
| `1234` | 图片 + 视频 + GIF + 音频 |

## 命令行用法

每个目标都可以单独指定下载类型，格式是：

```text
目标::下载类型
```

示例：

```powershell
python .\twitter_Crawler_2.py @user1::1 @user2::23 https://x.com/user3/media::4
```

也可以给所有目标设置统一下载类型：

```powershell
python .\twitter_Crawler_2.py @user1 @user2 @user3 -t 13
```

只提取音频，或显式设置 GIF 转换/保留 MP4：

```powershell
python .\twitter_Crawler_2.py @user1::4 --audio-format mp3
python .\twitter_Crawler_2.py @user1::3 --convert-gif --gif-fps 12 --gif-width 640
python .\twitter_Crawler_2.py @user1::3 --no-convert-gif
```

同时爬取两个用户：

```powershell
python .\twitter_Crawler_2.py @user1::1 @user2::1 --parallel-users 2
```

并行爬取会为每个用户启动一个独立 Chrome。建议先用 `2`，如果机器性能和账号状态稳定，再提高。

目标可以写：

- `user`
- `@user`
- `https://x.com/user/media`
- `https://twitter.com/user/media`

脚本会自动补全 `/media`，并把 `twitter.com` 转成 `x.com`。

其它参数：

```powershell
python .\twitter_Crawler_2.py --cookie X_cookie.json --output-dir D:\x_download --record downloaded_urls.json --failed-record failed_urls.json
```

## config.json 配置

默认读取当前目录的 `config.json`。最多支持 5 个目标。

`load_config` 先加载 `DEFAULT_CONFIG`，再用指定配置文件覆盖。代码缺省的 `parallel_users=1`、`gif_fps=8`、`gif_width=720`，当前随仓库提供的 `config.json` 分别为 `2`、`12`、`0`；桌面端还会传入已保存的任务选项。下面是配置示例，不是这三层配置的统一默认值；缺省值以当前源码为准。

示例：

```json
{
  "cookie_file": "X_cookie.json",
  "targets": [
    {
      "target": "@user1",
      "types": "1"
    },
    {
      "target": "https://x.com/user2/media",
      "types": "23"
    },
    {
      "target": "user3",
      "types": "123"
    },
    {
      "target": "",
      "types": ""
    },
    {
      "target": "",
      "types": ""
    }
  ],
  "download_types": "",
  "output_dir": ".",
  "download_record_file": "downloaded_urls.json",
  "failed_url_file": "failed_urls.json",
  "max_users": 5,
  "parallel_users": 2,
  "bootstrap_timeout": 20,
  "page_load_timeout": 30,
  "media_wait_timeout": 30,
  "max_idle_rounds": 6,
  "cells_per_round": 7,
  "stable_scroll_rounds": 2,
  "download_workers": 6,
  "request_connect_timeout": 10,
  "request_read_timeout": 60,
  "max_retries": 5,
  "use_system_proxy": true,
  "proxy_url": "",
  "retry_backoff_base": 1.0,
  "retry_backoff_max": 15.0,
  "image_format": "png",
  "audio_format": "mp3",
  "convert_gif": true,
  "keep_gif_mp4": false,
  "gif_fps": 12,
  "gif_width": 0,
  "rename_downloads": true,
  "delete_source_after_rename": true
}
```

配置说明：

- `cookie_file`：cookie 文件路径，默认 `X_cookie.json`。
- `targets`：目标列表，最多 5 个。空目标会被忽略。
- `targets[].target`：用户名、`@用户名` 或完整 URL。
- `targets[].types`：该目标自己的下载类型，优先级最高。
- `download_types`：全局默认下载类型。为空时，如果目标也没有写 `types`，运行时会询问。
- `output_dir`：下载输出目录。
- `download_record_file`：成功下载记录。相同 URL 对应的媒体文件仍存在时才跳过；新记录保存绝对路径。
- `failed_url_file`：失败 URL 清单。永久错误或有限重试后仍失败时会写入，之后下载成功会自动移除。
- `max_users`：最多目标数量，程序限制为 1 到 5。
- `parallel_users`：同时爬取的用户数，代码缺省为 1，仓库配置为 2。大于 1 时会启动多个 Chrome，任务数与媒体工作线程数是两层并发。
- `bootstrap_timeout`：打开 x.com 注入 cookie 的超时时间。
- `page_load_timeout`：打开目标页面的超时时间。
- `media_wait_timeout`：等待媒体列表出现的时间。
- `max_idle_rounds`：连续多少轮没有发现新资源后结束当前用户，默认 6。
- `cells_per_round`：每轮处理多少个页面内容块，默认 7。
- `stable_scroll_rounds`：页面高度连续稳定多少轮后才允许结束当前用户，默认 2。
- `download_workers`：固定下载 worker 数量，默认 6。资源多时不会再为每个 URL 新建线程。
- `request_connect_timeout`：下载请求连接超时。
- `request_read_timeout`：下载请求读取超时。
- `max_retries`：单个媒体请求的最多尝试次数，包含首次请求，当前至少尝试 1 次、最多 10 次；与桌面“失败自动重试”的整任务次数不同。
- `use_system_proxy`：请求未显式配置代理时是否读取 Windows 系统代理，代码缺省为 `true`。
- `proxy_url`：显式请求和下载浏览器代理，优先于请求的系统代理检测。Chrome 参数将 `socks5h` 表示为 `socks5`；浏览器未显式配置时保留系统代理行为。
- `retry_backoff_base` / `retry_backoff_max`：普通指数退避的基数和上限。有效 `Retry-After` 单独处理，不受普通退避上限截短，任务等待可取消。
- `image_format`：图片输出格式，默认 `png`；也支持 `jpg`、`webp` 或 `original`。
- `audio_format`：音频提取格式，支持 `m4a` 或 `mp3`，默认 `mp3`。
- `convert_gif`：是否把 GIF 媒体的 MP4 转换成 `.gif`，默认 `true`。转换比较耗 CPU 和时间。
- `keep_gif_mp4`：GIF 转换成功后是否保留源 MP4，默认 `false`。
- `gif_fps`：GIF 转换帧率，代码缺省 8，仓库配置为 12。
- `gif_width`：GIF 转换宽度，代码缺省 720，仓库配置为 `0`（保持原宽度）。
- `rename_downloads`：是否使用日期、推文 ID 和序号命名，缺省开启；当前作用于图片、视频及经视频流程处理的 GIF。
- `delete_source_after_rename`：缺省开启。关闭时复制到目标名称，保留原始名副本；不迁移已有文件，也不控制音频命名。

桌面任务另有统一的 `file_conflict` 策略：reuse、overwrite、error、rename。rename 在媒体输出锁内选择 `文件 (2).扩展名` 等新名，并把实际路径写入成功记录和下载库；不会被旧成功记录提前跳过。GIF/MP4 及可选原始副本按组选择新基名。此模式保留原始副本时，新副本使用所选基名加 `.original`，不改动已有原始名文件；该行为独立于日期命名开关。音频也支持冲突自动改名。

桌面端的 `save_unfinished_txt` 由 `service.py` 处理，独立 CLI 入口没有对应的未完成 TXT 开关。

## 优先级

下载类型优先级：

1. 命令行中的 `目标::类型`
2. `config.json` 中该目标的 `types`
3. 命令行 `-t / --types`
4. `config.json` 顶层 `download_types`
5. 都为空时交互输入

目标优先级：

1. 命令行传入的目标
2. `config.json` 中的 `targets`
3. 都为空时交互输入

只有在没有目标或缺少下载类型时，程序才会等待键盘输入。配置文件中每个目标都写好 `target` 和 `types` 时，不需要按任意键确认。

## 输出和记录

每个用户会创建自己的下载目录。视频和 GIF 会放入该用户目录下的 `video & gif` 文件夹，音频会放入 `audio` 文件夹。

开启重命名时，新下载的文件优先按推文上下文命名，例如 `20260718_120907_1234567890_img01.png`，格式是 `发帖时间_推文ID_资源序号`。如果页面里暂时拿不到推文 ID 或时间，会退回到顺序名或 URL 原始名，具体取决于提取路径。关闭重命名后，图片和视频按 URL 生成原始名称；旧文件不会自动迁移。

下载完成后会输出统计：

- 入队图片、视频、GIF、音频数量
- 成功下载数量
- 跳过数量，包括下载记录、本地已存在、队列重复
- 失败数量
- GIF 转换成功、跳过、失败数量

`downloaded_urls.json` 用于记录已经成功下载的 URL。下次运行时，只有记录指向的媒体文件仍存在才跳过；删除媒体不会抹掉历史，但允许重新下载。

`failed_urls.json` 用于记录永久错误或有限重试后仍失败的 URL。脚本不会把这些 URL 视为已完成；如果之后再次发现相同 URL，会重新尝试，成功后从失败清单移除。同一视频的音频使用独立记录键。

成功/失败记录、账号下载历史及隐藏历史使用规范化文件路径对应的操作系统文件锁；当前版本进程在锁内强制重读记录，修改后通过唯一临时文件原子替换。进程内共享缓存，提交成功后才发布；损坏记录会阻止更新和清空，保留原文件。记录锁等待最多 10 秒，进程终止后由系统释放。旧版及外部程序不遵守锁时不受保护，多个记录文件之间不具备整组原子提交。

Twitter 媒体请求对 401/403、404/410 等停止逐次重试，对 408/425/429、5xx 和请求异常保留有限重试；`Retry-After` 支持数字秒和 HTTP 日期。网络参数使用不可变快照，传入各个工作线程，不会由后启动任务改写前一个任务的请求配置。

## 注意事项

- `X_cookie.json` 等同登录凭证，只能用于你自己的账号，不要发送给别人。
- 如果 X 已提示“临时限制登录”，不要反复尝试登录。等限制解除后，再从普通浏览器复制 cookie。
- 独立 CLI 依赖 Selenium 打开页面并提取媒体；桌面服务另有公开单帖直连尝试，失败后回退浏览器，不能把此能力等同到独立 CLI。
- 音频提取和 GIF 转换优先使用 `ffmpeg`；如果没有 `ffmpeg`，会尝试用 `moviepy`，但建议安装 `ffmpeg`。
- 当前主要维护入口是 `twitter_Crawler_2.py`，旧 README 中提到的旧入口不再作为推荐方式。
