// GUI-subsystem host for unattended scheduled console actions. Never switches desktops.
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Xml;

internal static class HiddenLaunch
{
    const uint CREATE_NO_WINDOW = 0x08000000, STARTF_USESTDHANDLES = 0x100;
    const uint STARTF_USESHOWWINDOW = 1, INFINITE = 0xffffffff;
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct STARTUPINFO {
        public int cb; public string reserved, desktop, title;
        public uint x, y, xsize, ysize, xcount, ycount, fill, flags;
        public short show, reserved2; public IntPtr reservedPtr, input, output, error;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct PROCESS_INFORMATION { public IntPtr process, thread; public uint pid, tid; }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool CreateProcess(string application, StringBuilder command, IntPtr pa, IntPtr ta,
        bool inherit, uint flags, IntPtr environment, string directory,
        ref STARTUPINFO startup, out PROCESS_INFORMATION process);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool SetHandleInformation(IntPtr h, uint mask, uint flags);
    [DllImport("kernel32.dll")] static extern uint WaitForSingleObject(IntPtr h, uint ms);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool GetExitCodeProcess(IntPtr h, out uint code);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern IntPtr CreateFile(string name, uint access, uint share, IntPtr security, uint mode, uint attributes, IntPtr template);
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern IntPtr CreateDesktop(string desktop, string device, IntPtr mode, uint flags, uint access, IntPtr security);
    [DllImport("user32.dll")] static extern bool CloseDesktop(IntPtr h);

    static int Main(string[] args)
    {
        // Only the data file path is an argument; do not log command lines or credentials.
        if (args.Length != 1 || !Path.IsPathRooted(args[0])) return 125;
        IntPtr desktop = IntPtr.Zero;
        string resultPath = null;
        try {
            XmlReaderSettings settings = new XmlReaderSettings { DtdProcessing = DtdProcessing.Prohibit, XmlResolver = null };
            XmlDocument spec = new XmlDocument { XmlResolver = null };
            using (XmlReader reader = XmlReader.Create(args[0], settings)) spec.Load(reader);
            XmlElement root = spec.DocumentElement;
            if (root == null || root.Name != "hidden-launch" || root.GetAttribute("version") != "1") return 125;
            string logDir = root.GetAttribute("log-directory");
            if (!Path.IsPathRooted(logDir)) return 125;
            Directory.CreateDirectory(logDir);
            string runId = DateTime.UtcNow.ToString("yyyyMMddTHHmmssfffffffZ") + "-" + System.Diagnostics.Process.GetCurrentProcess().Id;
            resultPath = Path.Combine(logDir, runId + ".status");
            string desktopName = null;
            if (root.GetAttribute("isolate-desktop") == "true") {
                // Unattended console descendants cannot steal focus even if a tool omits windowsHide.
                // Same user/token/session; no service login, shell elevation or input-desktop switch.
                desktop = CreateDesktop("AgentStackBackground", null, IntPtr.Zero, 0, 0x01ff, IntPtr.Zero);
                if (desktop == IntPtr.Zero) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                desktopName = "WinSta0\\AgentStackBackground";
            }
            int actionIndex = 0, finalCode = 0;
            foreach (XmlNode node in root.ChildNodes) {
                XmlElement action = node as XmlElement;
                if (action == null || action.Name != "action") continue;
                string exe = Environment.ExpandEnvironmentVariables(action.GetAttribute("executable"));
                string arguments = Environment.ExpandEnvironmentVariables(action.GetAttribute("arguments"));
                string directory = Environment.ExpandEnvironmentVariables(action.GetAttribute("directory"));
                if (!Path.IsPathRooted(exe) || !File.Exists(exe)) throw new InvalidDataException("Executable unavailable");
                string command = "\"" + exe + "\"" + (arguments.Length == 0 ? "" : " " + arguments);
                string extension = Path.GetExtension(exe);
                if (extension.Equals(".cmd", StringComparison.OrdinalIgnoreCase) || extension.Equals(".bat", StringComparison.OrdinalIgnoreCase)) {
                    command = "\"" + exe + "\"" + (arguments.Length == 0 ? "" : " " + arguments);
                    exe = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "cmd.exe");
                    command = "\"" + exe + "\" /d /s /c \"" + command + "\"";
                }
                if (directory.Length == 0) directory = Environment.CurrentDirectory;
                IntPtr inHandle = CreateFile("NUL", 0x80000000, 3, IntPtr.Zero, 3, 0, IntPtr.Zero);
                if (inHandle == new IntPtr(-1)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                try {
                  using (FileStream output = new FileStream(Path.Combine(logDir, runId + "-" + actionIndex + ".log"), FileMode.CreateNew, FileAccess.Write, FileShare.Read)) {
                    IntPtr outHandle = output.SafeFileHandle.DangerousGetHandle();
                    if (!SetHandleInformation(inHandle, 1, 1) || !SetHandleInformation(outHandle, 1, 1))
                        throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                    STARTUPINFO startup = new STARTUPINFO { cb = Marshal.SizeOf(typeof(STARTUPINFO)), desktop = desktopName,
                        flags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW, show = 0, input = inHandle, output = outHandle, error = outHandle };
                    PROCESS_INFORMATION child;
                    if (!CreateProcess(exe, new StringBuilder(command), IntPtr.Zero, IntPtr.Zero, true, CREATE_NO_WINDOW,
                            IntPtr.Zero, directory, ref startup, out child))
                        throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                    CloseHandle(child.thread);
                    try {
                        File.AppendAllText(resultPath, "action=" + actionIndex + " pid=" + child.pid + " started\n");
                        if (WaitForSingleObject(child.process, INFINITE) != 0) throw new IOException("Wait failed");
                        uint exitCode;
                        if (!GetExitCodeProcess(child.process, out exitCode)) throw new IOException("Exit code unavailable");
                        finalCode = unchecked((int)exitCode);
                        File.AppendAllText(resultPath, "action=" + actionIndex + " exit=" + exitCode + "\n");
                    } finally { CloseHandle(child.process); }
                  }
                } finally { CloseHandle(inHandle); }
                actionIndex++;
            }
            return actionIndex == 0 ? 125 : finalCode;
        } catch (Exception ex) {
            if (resultPath != null) {
                try { File.AppendAllText(resultPath, "launcher-error=" + ex.GetType().Name + "\n"); } catch { }
            }
            return 126;
        } finally { if (desktop != IntPtr.Zero) CloseDesktop(desktop); }
    }
}
