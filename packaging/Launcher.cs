using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Windows.Forms;

class Launcher {
    static string Url;
    static int Port = 18700;
    static string Root, RootId;
    static bool NoDialog;
    static int Fail(string message, int code) {
        if (NoDialog) Console.Error.WriteLine(message);
        else MessageBox.Show(message, "Muse 微信管理台");
        return code;
    }
    static bool Healthy() {
        try {
            var request = (HttpWebRequest)WebRequest.Create(Url + "/health");
            request.Proxy = null; request.Timeout = 800; request.AllowAutoRedirect = false;
            using (var response = request.GetResponse())
            using (var reader = new StreamReader(response.GetResponseStream())) {
                var body = reader.ReadToEnd();
                return body.Contains("MuseWeChatManager") && body.Contains(RootId);
            }
        } catch { return false; }
    }
    static void Browse() { Process.Start(new ProcessStartInfo(Url) { UseShellExecute = true }); }
    [STAThread]
    static int Main(string[] args) {
        NoDialog = Array.IndexOf(args, "--no-dialog") >= 0;
        int portIndex = Array.IndexOf(args, "--port");
        if (portIndex >= 0 && (portIndex + 1 >= args.Length ||
            !Int32.TryParse(args[portIndex + 1], out Port) || Port < 1 || Port > 65535))
            return Fail("管理端口必须是 1 到 65535 的整数。", 6);
        Url = "http://127.0.0.1:" + Port;
        Root = Path.GetDirectoryName(System.Reflection.Assembly.GetExecutingAssembly().Location);
        using (var sha = SHA256.Create())
            RootId = BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(Root.ToLowerInvariant()))).Replace("-", "").ToLowerInvariant();
        bool created;
        using (var mutex = new Mutex(true, "Local\\MuseWeChatManager_" + RootId, out created)) {
            if (!created) {
                for (int i = 0; i < 30; i++) { if (Healthy()) { if (Array.IndexOf(args, "--no-browser") < 0) Browse(); return 0; } Thread.Sleep(200); }
                return Fail("软件正在启动，或管理端口已被其他程序占用。请稍后重试。", 2);
            }
            var python = Path.Combine(Root, "runtime-python", "python.exe");
            var script = Path.Combine(Root, "real", "manager_service.py");
            if (!File.Exists(python) || !File.Exists(script)) {
                return Fail("软件组件缺失。请完整解压软件包后，再打开启动器。", 3);
            }
            if (Healthy()) { if (Array.IndexOf(args, "--no-browser") < 0) Browse(); return 0; }
            try {
                var start = new ProcessStartInfo(python, "-I \"" + script + "\" --port " + Port) {
                    WorkingDirectory = Root, UseShellExecute = false,
                    CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden
                };
                start.EnvironmentVariables["MUSE_APP_ROOT"] = Root;
                using (var process = Process.Start(start)) {
                    bool ready = false;
                    for (int i = 0; i < 60; i++) {
                        if (process.HasExited) break;
                        if (Healthy()) { ready = true; break; }
                        Thread.Sleep(250);
                    }
                    if (!ready) {
                        if (!process.HasExited) process.Kill();
                        return Fail("管理器未能启动。请检查 " + Port + " 端口是否占用，以及软件包是否完整。", 4);
                    }
                    if (Array.IndexOf(args, "--no-browser") < 0) Browse();
                    process.WaitForExit();
                    return process.ExitCode;
                }
            } catch {
                return Fail("启动失败。请检查软件目录权限和 Windows 运行环境。", 5);
            } finally { mutex.ReleaseMutex(); }
        }
    }
}
