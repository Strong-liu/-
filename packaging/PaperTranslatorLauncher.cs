using System;
using System.Diagnostics;
using System.Collections.Generic;
using System.IO;
using System.Management;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;

[assembly: AssemblyTitle("橙意翻译助手")]
[assembly: AssemblyProduct("橙意翻译助手")]
[assembly: AssemblyDescription("PDFMathTranslate 与百度学术翻译桌面助手")]
[assembly: AssemblyVersion("0.3.2.0")]
[assembly: AssemblyFileVersion("0.3.2.0")]

internal static class PaperTranslatorLauncher
{
    private const string ProductName = "橙意翻译助手";
    private const string ProductVersion = "0.3.2";
    private const string ApplicationId = "paper-translator-desktop";
    private static readonly string DataDirectory = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "PaperTranslator");
    private static readonly string StateFile = Path.Combine(DataDirectory, "server.port");

    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Length > 0 && args[0] == "--version")
            return 0;
        bool stopForUpgrade = args.Length > 0 && args[0] == "--stop-for-upgrade";
        try
        {
            Directory.CreateDirectory(DataDirectory);
            if (stopForUpgrade && args.Length != 2)
                return 2;
            using (Mutex mutex = new Mutex(false, "Local\\PaperTranslatorDesktopLauncher"))
            {
                bool locked;
                try { locked = mutex.WaitOne(TimeSpan.FromSeconds(70), false); }
                catch (AbandonedMutexException) { locked = true; }
                if (!locked)
                    throw new InvalidOperationException("软件正在启动，请稍后重试。");
                try
                {
                    if (stopForUpgrade)
                        return StopOwnedServiceForUpgrade(args[1]);
                    string appDirectory = NormalizeInstallDirectory(AppDomain.CurrentDomain.BaseDirectory);
                    int port;
                    string existingHealth;
                    if (TryExistingServer(out port, out existingHealth))
                    {
                        if (IsCurrentVersion(existingHealth))
                        {
                            OpenBrowser(port);
                            return 0;
                        }
                        int stopResult = StopOwnedServiceForUpgrade(appDirectory);
                        if (stopResult != 0)
                            throw new InvalidOperationException(UpgradeFailureMessage(stopResult));
                        // Another installation may share this user-data folder.
                        // Never start a second version while that old service lives.
                        if (ApplicationHealth(port) != null)
                            throw new InvalidOperationException("检测到另一安装目录的旧版服务。请先关闭旧版服务，再打开橙意翻译助手。");
                    }

                    List<OwnedService> orphaned = FindOwnedServices(appDirectory);
                    if (orphaned.Count > 0)
                    {
                        foreach (OwnedService owned in orphaned)
                        {
                            string health = ApplicationHealth(owned.Port);
                            if (health != null && IsCurrentVersion(health))
                            {
                                File.WriteAllText(StateFile, owned.Port.ToString(), Encoding.ASCII);
                                OpenBrowser(owned.Port);
                                return 0;
                            }
                        }
                        int stopResult = StopOwnedServiceForUpgrade(appDirectory);
                        if (stopResult != 0)
                            throw new InvalidOperationException(UpgradeFailureMessage(stopResult));
                    }

                    string python = Path.Combine(appDirectory, "runtime", "pythonw.exe");
                    string entry = Path.Combine(appDirectory, "app.py");
                    if (!File.Exists(python) || !File.Exists(entry))
                        throw new FileNotFoundException("安装文件不完整，请重新安装软件。翻译记录和设置会保留在用户数据目录。\n" + appDirectory);

                    port = FindFreePort();
                    ProcessStartInfo start = new ProcessStartInfo();
                    start.FileName = python;
                    start.Arguments = "\"" + entry + "\" --port " + port;
                    start.WorkingDirectory = appDirectory;
                    start.UseShellExecute = false;
                    start.CreateNoWindow = true;
                    start.WindowStyle = ProcessWindowStyle.Hidden;
                    start.EnvironmentVariables["PYTHONUTF8"] = "1";
                    start.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
                    start.EnvironmentVariables["PYTHONNOUSERSITE"] = "1";
                    start.EnvironmentVariables["PYTHONPATH"] = Path.Combine(appDirectory, "engine")
                        + Path.PathSeparator + appDirectory;
                    start.EnvironmentVariables["PDF_TRANSLATOR_DATA_DIR"] = DataDirectory;
                    start.EnvironmentVariables["PDF_TRANSLATOR_PORT"] = port.ToString();
                    start.EnvironmentVariables["BABELDOC_CACHE_DIR"] = Path.Combine(DataDirectory, "cache", "babeldoc");
                    start.EnvironmentVariables["PDF_TRANSLATOR_ASSETS_DIR"] = Path.Combine(appDirectory, "assets");

                    using (Process process = Process.Start(start))
                    {
                        if (process == null)
                            throw new InvalidOperationException("无法启动内置运行环境。");
                        DateTime deadline = DateTime.UtcNow.AddSeconds(65);
                        while (DateTime.UtcNow < deadline)
                        {
                            string health = ApplicationHealth(port);
                            if (health != null && IsCurrentVersion(health))
                            {
                                File.WriteAllText(StateFile, port.ToString(), Encoding.ASCII);
                                OpenBrowser(port);
                                return 0;
                            }
                            if (process.HasExited)
                                throw new InvalidOperationException("翻译服务启动后退出，请查看：\n" + Path.Combine(DataDirectory, "server.log"));
                            Thread.Sleep(400);
                        }
                    }
                    throw new TimeoutException("启动等待超时，请查看：\n" + Path.Combine(DataDirectory, "server.log"));
                }
                finally { mutex.ReleaseMutex(); }
            }
        }
        catch (Exception error)
        {
            Log(error.ToString());
            if (!stopForUpgrade)
                MessageBox.Show(error.Message, ProductName + " — 启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    private static int FindFreePort()
    {
        TcpListener listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        int port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }

    private static bool TryExistingServer(out int port, out string health)
    {
        port = 0;
        health = null;
        try
        {
            if (!File.Exists(StateFile) || !Int32.TryParse(File.ReadAllText(StateFile).Trim(), out port))
                return false;
            health = ApplicationHealth(port);
            return health != null;
        }
        catch { return false; }
    }

    private static string GetLocalJson(int port, string route)
    {
        if (port < 1024 || port > 65535)
            throw new ArgumentOutOfRangeException("port");
        HttpWebRequest request = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + port + route);
        request.Proxy = null;
        request.Timeout = 2000;
        request.ReadWriteTimeout = 2000;
        using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
        using (StreamReader reader = new StreamReader(response.GetResponseStream(), Encoding.UTF8))
        {
            string body = reader.ReadToEnd();
            if (response.StatusCode != HttpStatusCode.OK || body.Length > 4 * 1024 * 1024)
                throw new InvalidOperationException("本机服务状态响应无效。");
            return body;
        }
    }

    private static string ApplicationHealth(int port)
    {
        try
        {
            string json = GetLocalJson(port, "/api/health");
            return JsonStringEquals(json, "app", ApplicationId) ? json : null;
        }
        catch { return null; }
    }

    private static bool JsonStringEquals(string json, string key, string value)
    {
        return Regex.IsMatch(json, "(?<!\\\\)\"" + Regex.Escape(key) + "\"\\s*:\\s*\"" + Regex.Escape(value) + "\"");
    }

    private static bool IsCurrentVersion(string health)
    {
        return JsonStringEquals(health, "version", ProductVersion);
    }

    private static bool HasActiveJobs(string jobs)
    {
        return Regex.IsMatch(jobs, "(?<!\\\\)\"status\"\\s*:\\s*\"(running|queued)\"");
    }

    private static int AssessIdleService(string health, string jobs)
    {
        if (health == null || jobs == null) return 21;
        jobs = jobs.Trim();
        if (!jobs.StartsWith("[", StringComparison.Ordinal) || !jobs.EndsWith("]", StringComparison.Ordinal))
            return 21;
        // A job may have started after the health request, so honor either
        // endpoint reporting activity rather than trusting a stale zero.
        if (HasActiveJobs(jobs)) return 20;
        Match count = Regex.Match(health, "(?<!\\\\)\"active_jobs\"\\s*:\\s*(\\d+)(?=\\s*[,}])");
        if (count.Success)
        {
            int active;
            if (!Int32.TryParse(count.Groups[1].Value, out active)) return 21;
            return active > 0 ? 20 : 0;
        }
        // New service with malformed full-count data is not safely idle.
        if (Regex.IsMatch(health, "(?<!\\\\)\"active_jobs\"\\s*:")) return 21;
        // The 0.2 history API caps results at 50. A full page cannot prove
        // that an older active translation was not omitted by that cap.
        int reportedJobs = Regex.Matches(jobs, "(?<!\\\\)\"status\"\\s*:").Count;
        if (reportedJobs >= 50) return 21;
        if (jobs != "[]" && reportedJobs == 0) return 21;
        return 0;
    }

    private static string NormalizeInstallDirectory(string directory)
    {
        if (String.IsNullOrWhiteSpace(directory))
            throw new ArgumentException("安装目录为空。");
        string resolved = Path.GetFullPath(directory).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        string root = Path.GetPathRoot(resolved).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        if (String.Equals(root, resolved, StringComparison.OrdinalIgnoreCase)
            || !Directory.Exists(resolved)
            || !File.Exists(Path.Combine(resolved, "app.py"))
            || !File.Exists(Path.Combine(resolved, "runtime", "pythonw.exe")))
            throw new ArgumentException("不是有效的橙意翻译助手安装目录。");
        return resolved;
    }

    private sealed class OwnedService
    {
        public int Pid;
        public int Port;
    }

    [DllImport("shell32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern IntPtr CommandLineToArgvW(string commandLine, out int count);
    [DllImport("kernel32.dll")]
    private static extern IntPtr LocalFree(IntPtr memory);

    private static string[] SplitCommandLine(string commandLine)
    {
        if (String.IsNullOrEmpty(commandLine)) return new string[0];
        int count;
        IntPtr arguments = CommandLineToArgvW(commandLine, out count);
        if (arguments == IntPtr.Zero) return new string[0];
        try
        {
            string[] values = new string[count];
            for (int i = 0; i < count; i++)
                values[i] = Marshal.PtrToStringUni(Marshal.ReadIntPtr(arguments, i * IntPtr.Size));
            return values;
        }
        finally { LocalFree(arguments); }
    }

    private static bool SamePath(string candidate, string expected)
    {
        try { return String.Equals(Path.GetFullPath(candidate), Path.GetFullPath(expected), StringComparison.OrdinalIgnoreCase); }
        catch { return false; }
    }

    private static List<OwnedService> FindOwnedServices(string directory)
    {
        string python = Path.Combine(directory, "runtime", "pythonw.exe");
        string script = Path.Combine(directory, "app.py");
        List<OwnedService> result = new List<OwnedService>();
        using (ManagementObjectSearcher searcher = new ManagementObjectSearcher(
            "SELECT ProcessId, ExecutablePath, CommandLine FROM Win32_Process WHERE Name='pythonw.exe'"))
        using (ManagementObjectCollection items = searcher.Get())
        {
            foreach (ManagementObject process in items)
            {
                string executable = process["ExecutablePath"] as string;
                string[] arguments = SplitCommandLine(process["CommandLine"] as string);
                if (!SamePath(executable, python)) continue;
                bool scriptMatches = false;
                int port = 0;
                for (int i = 1; i < arguments.Length; i++)
                {
                    if (SamePath(arguments[i], script)) scriptMatches = true;
                    if (arguments[i] == "--port" && i + 1 < arguments.Length)
                        Int32.TryParse(arguments[i + 1], out port);
                    else if (arguments[i].StartsWith("--port=", StringComparison.Ordinal))
                        Int32.TryParse(arguments[i].Substring(7), out port);
                }
                if (scriptMatches)
                    result.Add(new OwnedService { Pid = Convert.ToInt32(process["ProcessId"]), Port = port });
            }
        }
        return result;
    }

    private static int StopOwnedServiceForUpgrade(string targetDirectory)
    {
        string directory = NormalizeInstallDirectory(targetDirectory);
        List<OwnedService> services = FindOwnedServices(directory);
        // Verify every target before stopping any process. An unavailable jobs
        // endpoint is not permission to kill a possibly active translation.
        foreach (OwnedService service in services)
        {
            string health = ApplicationHealth(service.Port);
            if (health == null)
            {
                Log("Upgrade stop refused: could not verify owned service health, PID=" + service.Pid);
                return 21;
            }
            string jobs;
            try { jobs = GetLocalJson(service.Port, "/api/jobs").Trim(); }
            catch { return 21; }
            int idleResult = AssessIdleService(health, jobs);
            if (idleResult != 0)
            {
                Log("Upgrade stop refused: activity or incomplete idle proof, PID=" + service.Pid + ", result=" + idleResult);
                return idleResult;
            }
        }
        foreach (OwnedService service in services)
        {
            // Re-query identity immediately before taskkill to guard PID reuse.
            bool stillOwned = FindOwnedServices(directory).Exists(delegate(OwnedService item) { return item.Pid == service.Pid && item.Port == service.Port; });
            if (!stillOwned) continue;
            try
            {
                // Check once more after identity verification so a newly queued
                // job cancels the stop as late as possible.
                int idleResult = AssessIdleService(ApplicationHealth(service.Port), GetLocalJson(service.Port, "/api/jobs"));
                if (idleResult != 0) return idleResult;
            }
            catch { return 21; }
            ProcessStartInfo stop = new ProcessStartInfo(Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.System), "taskkill.exe"),
                "/PID " + service.Pid + " /T /F");
            stop.UseShellExecute = false;
            stop.CreateNoWindow = true;
            stop.WindowStyle = ProcessWindowStyle.Hidden;
            stop.RedirectStandardOutput = true;
            stop.RedirectStandardError = true;
            using (Process process = Process.Start(stop))
            {
                if (process == null || !process.WaitForExit(15000)) return 22;
                if (process.ExitCode != 0 && FindOwnedServices(directory).Exists(
                    delegate(OwnedService item) { return item.Pid == service.Pid; })) return 22;
            }
            Log("Stopped verified idle owned service for upgrade, PID=" + service.Pid);
        }
        return 0;
    }

    private static string UpgradeFailureMessage(int code)
    {
        if (code == 20) return "旧版服务仍有正在翻译或排队的任务。请等待任务完成，再打开新版；当前任务未被中断。";
        if (code == 21) return "无法确认旧版服务是否空闲，已取消自动重启。请检查旧版页面和运行日志后重试。";
        return "无法关闭旧版服务，请关闭该软件后重试。其他 Python 程序未被操作。";
    }

    private static void Log(string message)
    {
        try { File.AppendAllText(Path.Combine(DataDirectory, "launcher.log"), DateTime.Now.ToString("s") + " " + message + Environment.NewLine, Encoding.UTF8); }
        catch { }
    }

    private static void OpenBrowser(int port)
    {
        ProcessStartInfo browser = new ProcessStartInfo("http://127.0.0.1:" + port + "/");
        browser.UseShellExecute = true;
        Process.Start(browser);
    }
}
