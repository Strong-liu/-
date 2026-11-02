# 橙意翻译助手：Windows 构建

The installer targets Windows 10/11 x64. It bundles a relocatable CPython 3.12 runtime, the installed PDFMathTranslate dependencies, the PDFMathTranslate source tree, and the BabelDOC fonts/layout model/CMaps/tiktoken assets. No Python installation or developer machine path is required after installation.

## 0.3.2 构建输入

构建在 Windows x64 上进行，使用 Python 3.12、Windows 自带的 .NET Framework C# 编译器和 Inno Setup 6.4.3 或兼容版本。生产运行环境与开发虚拟环境分开：虚拟环境本身包含指向系统 Python 的路径，不能直接作为独立运行环境分发。

先准备 Python 依赖并应用固定版本的兼容修补：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe packaging\patch_runtime.py .venv\Lib\site-packages
```

下载字体、版面模型、CMap 和分词资源到标准构建目录（此步骤需要网络，不需要百度凭据）：

```powershell
$env:BABELDOC_CACHE_DIR = Join-Path $PWD 'packaging\deps\babeldoc'
.\.venv\Scripts\python.exe -c "from babeldoc.assets.assets import warmup; warmup()"
```

其余构建输入：

- `packaging/deps/python/`：可再分发的完整 CPython 3.12 Windows x64 目录，含 `python.exe`、`pythonw.exe`、`Lib/`、`DLLs/` 和 `LICENSE.txt`。使用完整运行环境，而非开发 `.venv` 或仅含 stdlib ZIP 的嵌入式分发。
- `packaging/deps/vc-app-local/`：从 [微软官方 Visual C++ 运行库](https://aka.ms/vs/17/release/vc_redist.x64.exe) 获取的 AMD64 app-local DLL；随包保留微软许可。实际已验证的 DLL 名称包括 `msvcp140*`、`vcruntime140*`、`concrt140.dll`、`vcomp140.dll`、`vcamp140.dll`、`vccorlib140.dll`。
- Inno 编译器：安装 Inno Setup 并将 `ISCC.exe` 加入 PATH，或设置 `CHENGYI_INNO_COMPILER`。本机开发缓存中的 `packaging/toolchain/` 也可复用，但不提交 Git。
- 可选自有图标：默认 `assets/branding/app.ico`，也可用 `-IconSource` 指定。PNG/JPEG 等位图可先运行 `python packaging/make_icon.py 原图.png assets/branding/app.ico` 转成多尺寸 ICO。转换保留原图，等比缩放并填充透明边距；未提供图标时使用 Windows 标准图标，不生成替代设计。

上述输入目录可分别用参数 `-RuntimeSource`、`-DependencySource`、`-AssetsSource`、`-VcRuntimeSource`、`-Compiler` 覆盖；对应环境变量为 `CHENGYI_RUNTIME_SOURCE`、`CHENGYI_DEPENDENCY_SOURCE`、`CHENGYI_ASSETS_SOURCE`、`CHENGYI_VC_RUNTIME_SOURCE`、`CHENGYI_INNO_COMPILER`。引擎源码默认取仓库的 `engine/`。

```powershell
.\packaging\build.ps1
```

已有 `build/payload/` 时使用 `-SkipPrepare` 复用运行环境，但会重新同步应用、引擎、UI 与兼容补丁。`-PrepareOnly` 只准备环境并编译启动器。完整安装包输出到 `dist/ChengyiTranslationAssistant-0.3.2-Windows-x64-Setup.exe`。

`patch_runtime.py` 固定检查 BabelDOC 0.6.4；可读的排版修补源码在 `patches/`，其上游许可随源码与安装目录保留。百度新 Key 模式直接通过 HTTPS 读取官方接口的全部结果，不需要 `trans.exe`。

升级保留原 AppId 与 `%LOCALAPPDATA%\PaperTranslator`。安装器调用独立启动器的升级模式，仅处理其安装目录中身份和命令行均匹配的服务；发现仍在翻译或排队时会阻止升级，避免中断用户任务。开发者打包前也应结束自己启动的本地调试进程。

0.3.2 的速度优化位于应用与 `engine_support/` 源码中，无需替换运行环境或重新下载模型。复用 payload 时仍须同步这些源码和前端资源，并重新编译版本为 0.3.2 的启动器。沿用原图标、安装目录和用户数据目录，已有完整译文缓存继续复用。

## Payload layout

```
PaperTranslator.exe
app.py
static/
engine_support/
engine/pdf2zh_next/
runtime/python.exe
runtime/pythonw.exe
runtime/Lib/site-packages/
assets/fonts/
assets/models/
assets/cmap/
assets/tiktoken/
```

Build preparation:

```
./packaging/build.ps1 -PrepareOnly
```

Final build after app/static/engine_support and INSTALLATION_NOTES.txt are ready:

```
./packaging/build.ps1 -SkipPrepare
```

The default Inno Setup compiler path is `packaging/toolchain/inno-6.4.3/{app}/ISCC.exe`. The compiler was extracted from the official signed Inno Setup installer instead of installing a system-wide tool. Compiler tooling is not shipped to end users.

The launcher is an x64 Windows application compiled with .NET Framework 4, which is included in supported Windows 10/11 installations. It starts the bundled pythonw.exe with a random loopback port, checks `/api/health`, and opens the default browser. Service status and user data live under `%LocalAppData%/PaperTranslator`, not the installation directory. The app health response must contain `paper-translator-desktop`.

The copied BabelDOC const.py accepts the explicit BABELDOC_CACHE_DIR environment variable. The shipped fonts and models contain no translated-text databases or developer credentials. App startup should copy these bundled assets to the per-user cache before invoking the engine.

The installer installs per-user to `%LocalAppData%/Programs/PaperTranslator`, adds Start menu and optional desktop shortcuts, and includes an uninstaller. User-generated documents and settings are retained on uninstall. It does not require elevation and does not install a system service.

Upstream sources:

- PDFMathTranslate: https://github.com/PDFMathTranslate-next/PDFMathTranslate-next
- Inno Setup 6.4.3: https://github.com/jrsoftware/issrc/releases/tag/is-6_4_3
- Python runtime license: included as PYTHON_LICENSE.txt

Each dependency's bundled dist-info/license files and PDFMathTranslate's AGPL-3.0 license remain included in the installed payload. The PDFMathTranslate source and local Python application source are distributed as readable files.
