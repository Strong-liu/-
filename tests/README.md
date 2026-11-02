# 测试说明

离线测试通过 `unittest` 运行，不会发送百度请求。API 测试在临时目录生成合成 PDF；测试用虚拟凭据与正式设置隔离。Windows DPAPI 凭据测试需在 Windows 上运行。

```powershell
python -m unittest discover -s engine_support -p "test_*.py"
python -m unittest discover -s tests -p "test_*.py"
# 本地应用已启动时，隔离 API 的浏览器状态测试（不创建或取消真实任务）
node tests/status_ui_smoke.mjs http://127.0.0.1:8766
```

速度相关回归使用模拟接口验证批量恢复的请求数、片段边界分配、拆分处理、缓存复用、致命错误短路和共享限速；运行器测试检查网络等待并发与心跳清理。API 测试覆盖任务进度、静默监控及取消行为。这些检查不以减弱术语保护或完整性校验换取速度。

性能测试中的模拟耗时与请求数用于验证调度行为，不代表真实论文的完成时间。真实文档计时应同时记录页数、QPS、缓存命中、请求数、重试次数和输出校验结果；复用缓存与首次翻译的结果应分别说明。

## 可选完整引擎离线测试

已安装完整运行依赖并下载 BabelDOC 资源时，可验证真实 PDF 解析、翻译调度和排版流程。此测试使用合成 PDF 与模拟百度接口，不读取个人凭据，不调用真实翻译服务。

```powershell
python tests/engine_reference_smoke.py --assets packaging/deps/babeldoc
```

测试包含带编号和字间空格的参考文献标题、IEEE 格式文献、跨页文献及附录；验证所有文献原文保留、正文和附录仍进入翻译、旧任务关闭参考文献保护时也会自动跳过。结果写入 `build/engine-reference-smoke/`。

## 可选在线浏览器测试

此测试需要已启动的应用，以及该应用本机已配置的百度大模型翻译 Key。测试会调用真实百度服务，可能计入账户额度。

```powershell
npm ci
npx playwright install chromium
$env:PAPER_TRANSLATOR_PYTHON = (Resolve-Path .\.venv\Scripts\python.exe).Path
node tests/e2e_smoke.mjs http://127.0.0.1:8766
```

默认在 `build/e2e-fixtures/` 生成一页合成英文论文，测试上传、审核专名、翻译、双版本下载、作者和期刊名恢复、刷新及窄屏布局。截图、PDF 和结果写入 `build/e2e-results/`，不提交仓库。

可选环境变量：

- `PAPER_TRANSLATOR_PYTHON`：已安装 PyMuPDF 的 Python 可执行文件。
- `PLAYWRIGHT_CHROME_PATH`：已有 Chrome 的完整路径；不设置时使用 Playwright Chromium。
- `E2E_PDF_PATH`：自备测试 PDF，替代自动生成的合成文档。
- `E2E_EXPECTED_TERMS`：JSON 字符串数组，指定前页应保留的英文名称。
- `E2E_TIMEOUT_MS`：等待翻译完成的最长时间，默认 480000 毫秒。

请将真实论文及测试诊断保留在本地，避免提交个人文件或 API 凭据。
