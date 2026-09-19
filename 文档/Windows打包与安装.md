# Windows 打包与安装

## 构建准备

使用 64 位 Python，在项目环境安装运行依赖与构建依赖：

```powershell
python -m pip install -r .\requirements-app.txt
python -m pip install -r .\requirements-build.txt
```

项目根目录必须保留官方 `aria2-1.37.0-win-64bit-build1.zip`。构建脚本会先验证固定 SHA-256；不匹配会直接停止。原始 ZIP 连同 `COPYING` 等上游许可内容一起进入发行目录，不能改成来历不明的二进制。

桌面版、CLI 和安装程序统一使用根目录 `蔑视.png`。构建脚本会先校验这张 2000×2000 RGBA 方形原图，并生成包含 16、24、32、48、64、128、256 像素图层的 `packaging\欲求达.ico`，无需手工转换。

## 构建 EXE 目录

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows.ps1
```

输出位于 `dist\欲求达\`：

- `欲求达.exe`：无控制台的桌面程序。
- `欲求达-cli.exe`：诊断命令，例如 `browser-driver`、`modules`、`aria2`。
- `_internal\`：Python、Tk、依赖 DLL、FFmpeg、文档和 aria2 官方 ZIP。必须与 EXE 一起分发，不能只复制单个 EXE。

使用 `onedir` 是有意选择：Tkinter、Selenium、`curl_cffi` 和媒体依赖包含原生 DLL；目录构建启动更快，也不会每次运行把整套程序解压到临时目录。

发行构建只收集 ImageIO-FFmpeg 提供的 FFmpeg 可执行文件，不把开发环境里未使用的 Torch、SciPy、Pandas、Jupyter 等可选插件带入安装包。运行时启动钩子把内置 FFmpeg 工具目录加入当前进程 PATH，图片/视频/音频转换仍走现有 FFmpeg 代码路径。

## 生成安装程序

安装 Inno Setup 6 后打开 `packaging\欲求达.iss` 编译，或执行：

```powershell
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" .\packaging\欲求达.iss
```

安装包输出到 `dist\installer\`，采用当前用户安装，不要求管理员权限。程序文件位于 `%LOCALAPPDATA%\Programs\YuqiuDa`；数据库、Cookie、浏览器资料、aria2 和 ChromeDriver 位于 `%LOCALAPPDATA%\YuqiuDa\data\software_app`，因此覆盖安装或卸载程序不会自动删除登录资料。默认下载目录为用户的 `Downloads\欲求达`。

需要便携数据目录时，可在启动前设置 `YUQIUDA_HOME` 为一个可写的绝对目录。

## aria2 与 ChromeDriver

- aria2 不会随启动自动运行。用户在设置页启用 BT 并点击安装时，软件优先读取发行包内的官方 ZIP，验证 SHA-256 后把 `aria2c.exe` 与许可文件安装到用户数据目录；本地包缺失时才联网下载。
- ChromeDriver 不固定打进安装包，因为它必须匹配用户电脑上的 Chrome。设置页“检测浏览器驱动”会实际运行 Driver 并对比版本；缺失、损坏或主版本不一致时，点击“安装 / 更新驱动”会从 Chrome for Testing 自动匹配下载并原子替换，用户不需要选择版本。系统 PATH 中已有的匹配驱动会迁入软件数据目录复用。
- 普通 Chrome 本身不随软件分发，仍需用户安装 Chrome 或兼容的 Chromium 浏览器。

## 发布前检查

```powershell
.\dist\欲求达\欲求达-cli.exe modules
.\dist\欲求达\欲求达-cli.exe browser-driver
.\dist\欲求达\欲求达-cli.exe browser-driver --download
.\dist\欲求达\欲求达-cli.exe aria2 --install
```

随后启动桌面版，检查设置页、下载库、Cookie 文件导入和输出目录。不要把开发机的 `data\`、Cookie、SQLite、下载媒体或 ChromeDriver 放入安装包。
