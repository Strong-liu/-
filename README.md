# 橙意翻译助手

<img src="static/branding/app-icon.png" alt="橙意翻译助手图标" width="96" align="right">

面向 Windows 10/11 x64 的论文 PDF 翻译软件，基于 PDFMathTranslate 与 BabelDOC。在本机解析 PDF、识别版面并生成中文版和中英对照版，待翻译文字通过百度官方接口翻译。

## 安装与使用

Windows 安装包发布在 [Releases](https://github.com/Strong-liu/-/releases)。安装版包含 Python、翻译引擎、字体和版面模型；安装后打开桌面上的“橙意翻译助手”，软件会自动打开本机操作页面。

1. 在“翻译设置”填写百度翻译 API 凭据。
2. 上传包含可复制英文文字的 PDF，核对保留英文的术语清单。
3. 开始翻译，完成后下载中文 PDF 或中英对照 PDF。

作者、期刊、模型和方法名按照术语清单保留英文，清单可增删。自动识别可能遗漏或误判，重要论文请核对清单及译文。参考文献自动跳过，整体保留原文；扫描件需要先进行 OCR。

## 百度服务与密钥

- **百度大模型翻译 · 学术风格**：使用百度翻译 API Key。
- **百度学术论文领域 API**：使用专门开通该服务的 APPID 和密钥，固定 `domain=academic`。

这两种服务的凭据不同；网页会员不等同于 API 权限。免费额度和计费以百度账户控制台显示为准。在线翻译需要联网。

Windows 安装版用当前用户的 DPAPI 加密保存凭据。个人设置、历史、缓存和输出位于 `%LOCALAPPDATA%\PaperTranslator`，升级会继续使用该目录。安装包和源码仓库不包含个人密钥或论文。

## 翻译完整性

百度可能把一段请求拆成多条 `trans_result` 返回。软件读取全部记录并校验原文覆盖范围，随后校验和恢复英文专名。遇到服务错误、标记损坏或排版失败时，会明确报告问题，完整性检查通过后才开放下载。

0.3.0 修复了旧版命令行适配器只读取第一条返回记录造成的截断问题，并隔离旧版大模型翻译缓存。失败时显示的翻译错误数和排版错误数分别统计。

0.3.1 在 PDF 引擎生成公式占位符之前识别并跳过参考文献，旧任务中的参考文献选项也统一按保留原文处理。术语恢复不再设 40 片段数量上限；仍按百度服务允许的请求大小分块、遵守 QPS，并校验每个响应的完整性。

## 翻译速度与任务状态

0.3.2 将术语校验失败后的短片段合并请求，按百度返回的原文记录逐个验证边界，再把保护词在本机插回原位置。若返回记录跨越片段边界，程序拆分重试；不会按换行猜测译文对应关系。相同恢复片段会去重并缓存，减少重复请求。

网络等待可以并发进行：默认 QPS 为 1 时使用 8 个翻译工作线程，仍由同一个限速器控制每秒请求数。线程数按 QPS 调整到 4–16 个，不会自动提高账户 QPS。页数、术语密度、百度响应时间及账户额度都会影响总耗时。

翻译页面显示已处理段数、总段数、耗时、请求及重试状态，可以取消当前任务或排队任务。任务没有固定总时长上限；连续 5 分钟没有 PDF 进度或翻译网络活动时会停止并明确报错。取消或失败后可重试，已通过校验的缓存会继续使用。

## 从源码运行

Windows、Python 3.12：

```powershell
git clone https://github.com/Strong-liu/-.git chengyi-translation-assistant
cd chengyi-translation-assistant
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe packaging\patch_runtime.py .venv\Lib\site-packages
$env:BABELDOC_CACHE_DIR = Join-Path $PWD 'packaging\deps\babeldoc'
.\.venv\Scripts\python.exe -c "from babeldoc.assets.assets import warmup; warmup()"
$env:PDF_TRANSLATOR_ASSETS_DIR = $env:BABELDOC_CACHE_DIR
.\.venv\Scripts\python.exe app.py --port 8766
```

浏览器打开 `http://127.0.0.1:8766`。生产安装版附带版面资源；源码运行也需要配置 BabelDOC 字体和模型资源。资源准备、依赖修补与独立安装包的生成步骤见 [Windows 构建说明](packaging/README.md)。

安装版监听本机随机端口，不需要手工维护终端。源码开发模式下，终端进程需保持运行。

## 测试

离线测试使用模拟百度响应和自动生成的 PDF，不需要 API Key，也不包含私人论文：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-tests.txt
.\.venv\Scripts\python.exe -m unittest discover -s engine_support -p "test_*.py"
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

浏览器端到端测试为可选在线测试，会使用本机已配置的百度账户，可能计入额度。运行方式和参数见 [tests/README.md](tests/README.md)。

## 开源许可与上游

本项目按 [GNU AGPL-3.0](LICENSE) 发布。第三方组件保留各自许可，说明见 [第三方软件清单](packaging/THIRD_PARTY_NOTICES.md)。

- [PDFMathTranslate-next](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next)，版本 2.9.0；对应源码与许可位于 `engine/`。
- [BabelDOC](https://github.com/funstory-ai/BabelDOC)，版本 0.6.4；安装依赖时获取，兼容修补随构建源码提供。

软件名称及图标用于标识本项目；第三方商标仍归其各自权利人所有。
