[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$CurrentRoot = Split-Path -Parent $PSScriptRoot
$Root = Split-Path -Parent $CurrentRoot
$RequestPath = Join-Path $Root 'state\browser.lease\desktop-request.json'
$RunnerVersion = '0.3.0'

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Initialize-AtomicFileType {
    if ('RemoteComputerUse.AtomicFile' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace RemoteComputerUse {
    public static class AtomicFile {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern bool MoveFileEx(string existingFile, string newFile, uint flags);
    }
}
'@
}

function Write-JsonAtomic([string]$Path, $Value) {
    $temporary = $Path + '.tmp-' + $PID
    Write-Utf8NoBom $temporary (($Value | ConvertTo-Json -Depth 20) + "`n")
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        Initialize-AtomicFileType
        $replaceExistingAndWriteThrough = [uint32]0x9
        if (-not [RemoteComputerUse.AtomicFile]::MoveFileEx($temporary, $Path, $replaceExistingAndWriteThrough)) {
            $nativeError = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
            throw (New-Object System.ComponentModel.Win32Exception -ArgumentList $nativeError)
        }
    }
    else {
        Move-Item -LiteralPath $temporary -Destination $Path
    }
}

function Test-PathUnderRoot([string]$Path, [string]$ExpectedRoot) {
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $fullRoot = [System.IO.Path]::GetFullPath($ExpectedRoot).TrimEnd('\')
    return $fullPath.StartsWith($fullRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-AppPolicy($Config, [string]$AppId) {
    return @($Config.desktop_allowed_apps | Where-Object { $_.id -eq $AppId }) | Select-Object -First 1
}

function Initialize-DesktopNativeType {
    if ('RemoteComputerUse.DesktopNative' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

namespace RemoteComputerUse {
    [StructLayout(LayoutKind.Sequential)]
    public struct MOUSEINPUT {
        public int dx;
        public int dy;
        public uint mouseData;
        public uint dwFlags;
        public uint time;
        public IntPtr dwExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct KEYBDINPUT {
        public ushort wVk;
        public ushort wScan;
        public uint dwFlags;
        public uint time;
        public IntPtr dwExtraInfo;
    }

    [StructLayout(LayoutKind.Explicit)]
    public struct INPUTUNION {
        [FieldOffset(0)] public MOUSEINPUT mi;
        [FieldOffset(0)] public KEYBDINPUT ki;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct INPUT {
        public uint type;
        public INPUTUNION U;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct RECT {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct IO_COUNTERS {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_BASIC_LIMIT_INFORMATION {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    public static class DesktopNative {
        private delegate bool EnumWindowsProc(IntPtr window, IntPtr parameter);
        private const uint INPUT_MOUSE = 0;
        private const uint INPUT_KEYBOARD = 1;
        private const uint KEYEVENTF_KEYUP = 0x0002;
        private const uint KEYEVENTF_UNICODE = 0x0004;
        private const uint MOUSEEVENTF_LEFTDOWN = 0x0002;
        private const uint MOUSEEVENTF_LEFTUP = 0x0004;
        private const uint MOUSEEVENTF_RIGHTDOWN = 0x0008;
        private const uint MOUSEEVENTF_RIGHTUP = 0x0010;
        private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;

        [DllImport("user32.dll")]
        private static extern bool SetProcessDPIAware();

        [DllImport("user32.dll", SetLastError = true)]
        private static extern uint SendInput(uint count, INPUT[] inputs, int size);

        [DllImport("user32.dll", SetLastError = true)]
        private static extern bool SetCursorPos(int x, int y);

        [DllImport("user32.dll")]
        private static extern IntPtr GetForegroundWindow();

        [DllImport("user32.dll")]
        private static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);

        [DllImport("user32.dll")]
        private static extern bool SetForegroundWindow(IntPtr window);

        [DllImport("user32.dll")]
        private static extern bool ShowWindowAsync(IntPtr window, int command);

        [DllImport("user32.dll", SetLastError = true)]
        private static extern bool GetWindowRect(IntPtr window, out RECT rectangle);

        [DllImport("user32.dll")]
        private static extern bool EnumWindows(EnumWindowsProc callback, IntPtr parameter);

        [DllImport("user32.dll")]
        private static extern bool IsWindowVisible(IntPtr window);

        [DllImport("kernel32.dll")]
        private static extern uint WTSGetActiveConsoleSessionId();

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateJobObject(IntPtr attributes, string name);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetInformationJobObject(IntPtr job, int infoClass, IntPtr info, uint infoLength);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr handle);

        public static void EnableDpiAwareness() {
            SetProcessDPIAware();
        }

        public static uint ActiveConsoleSessionId() {
            return WTSGetActiveConsoleSessionId();
        }

        public static IntPtr CreateKillOnCloseJob() {
            IntPtr job = CreateJobObject(IntPtr.Zero, null);
            if (job == IntPtr.Zero) {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "CreateJobObject failed");
            }
            var limits = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            int length = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
            IntPtr buffer = Marshal.AllocHGlobal(length);
            try {
                Marshal.StructureToPtr(limits, buffer, false);
                if (!SetInformationJobObject(job, 9, buffer, (uint)length)) {
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "SetInformationJobObject failed");
                }
                return job;
            } catch {
                CloseHandle(job);
                throw;
            } finally {
                Marshal.FreeHGlobal(buffer);
            }
        }

        public static void AssignToJob(IntPtr job, IntPtr process) {
            if (!AssignProcessToJobObject(job, process)) {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "AssignProcessToJobObject failed");
            }
        }

        public static void CloseJob(IntPtr job) {
            if (job != IntPtr.Zero) CloseHandle(job);
        }

        public static uint ForegroundProcessId() {
            uint processId;
            GetWindowThreadProcessId(GetForegroundWindow(), out processId);
            return processId;
        }

        public static bool Activate(IntPtr window) {
            if (window == IntPtr.Zero) return false;
            ShowWindowAsync(window, 9);
            return SetForegroundWindow(window);
        }

        public static int[] LargestVisibleWindowRectangle(uint expectedProcessId) {
            RECT best = new RECT();
            long bestArea = 0;
            EnumWindows((window, parameter) => {
                uint processId;
                GetWindowThreadProcessId(window, out processId);
                if (processId != expectedProcessId || !IsWindowVisible(window)) return true;
                RECT rectangle;
                if (!GetWindowRect(window, out rectangle)) return true;
                long width = Math.Max(0, rectangle.Right - rectangle.Left);
                long height = Math.Max(0, rectangle.Bottom - rectangle.Top);
                long area = width * height;
                if (area > bestArea) {
                    best = rectangle;
                    bestArea = area;
                }
                return true;
            }, IntPtr.Zero);
            if (bestArea == 0) {
                throw new InvalidOperationException("No visible window was found for the target process");
            }
            return new[] { best.Left, best.Top, best.Right, best.Bottom };
        }

        public static void MovePointer(int x, int y) {
            if (!SetCursorPos(x, y)) {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "SetCursorPos failed");
            }
        }

        private static void SendMouse(uint flags) {
            var input = new INPUT {
                type = INPUT_MOUSE,
                U = new INPUTUNION {
                    mi = new MOUSEINPUT { dwFlags = flags }
                }
            };
            if (SendInput(1, new[] { input }, Marshal.SizeOf(typeof(INPUT))) != 1) {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "mouse SendInput failed");
            }
        }

        public static void Click(string button, int count) {
            uint down = button == "right" ? MOUSEEVENTF_RIGHTDOWN : MOUSEEVENTF_LEFTDOWN;
            uint up = button == "right" ? MOUSEEVENTF_RIGHTUP : MOUSEEVENTF_LEFTUP;
            for (int i = 0; i < count; i++) {
                SendMouse(down);
                SendMouse(up);
                if (count > 1) System.Threading.Thread.Sleep(80);
            }
        }

        private static void SendKeyboard(ushort virtualKey, ushort scanCode, uint flags) {
            var input = new INPUT {
                type = INPUT_KEYBOARD,
                U = new INPUTUNION {
                    ki = new KEYBDINPUT { wVk = virtualKey, wScan = scanCode, dwFlags = flags }
                }
            };
            if (SendInput(1, new[] { input }, Marshal.SizeOf(typeof(INPUT))) != 1) {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "keyboard SendInput failed");
            }
        }

        public static int SendText(string value) {
            const int chunkCharacters = 128;
            for (int offset = 0; offset < value.Length; offset += chunkCharacters) {
                int count = Math.Min(chunkCharacters, value.Length - offset);
                var inputs = new INPUT[count * 2];
                for (int index = 0; index < count; index++) {
                    ushort codeUnit = value[offset + index];
                    inputs[index * 2] = new INPUT {
                        type = INPUT_KEYBOARD,
                        U = new INPUTUNION {
                            ki = new KEYBDINPUT { wScan = codeUnit, dwFlags = KEYEVENTF_UNICODE }
                        }
                    };
                    inputs[index * 2 + 1] = new INPUT {
                        type = INPUT_KEYBOARD,
                        U = new INPUTUNION {
                            ki = new KEYBDINPUT { wScan = codeUnit, dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP }
                        }
                    };
                }
                uint sent = SendInput((uint)inputs.Length, inputs, Marshal.SizeOf(typeof(INPUT)));
                if (sent != (uint)inputs.Length) {
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "text SendInput was incomplete");
                }
                System.Threading.Thread.Sleep(5);
            }
            return value.Length;
        }

        public static void KeyDown(ushort virtualKey) {
            SendKeyboard(virtualKey, 0, 0);
        }

        public static void KeyUp(ushort virtualKey) {
            SendKeyboard(virtualKey, 0, KEYEVENTF_KEYUP);
        }
    }
}
'@
}

function Save-DesktopScreenshot([string]$Path) {
    Add-Type -AssemblyName System.Drawing
    Add-Type -AssemblyName System.Windows.Forms
    $bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
    if ($bounds.Width -le 0 -or $bounds.Height -le 0) {
        throw 'Interactive desktop has invalid screen bounds'
    }
    $bitmap = New-Object System.Drawing.Bitmap($bounds.Width, $bounds.Height)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    try {
        $graphics.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bounds.Size)
        $bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    }
    finally {
        $graphics.Dispose()
        $bitmap.Dispose()
    }
    return $bounds
}

function Get-ForegroundProcessInfo {
    $processId = [int][RemoteComputerUse.DesktopNative]::ForegroundProcessId()
    $process = $(if ($processId -gt 0) { Get-Process -Id $processId -ErrorAction SilentlyContinue } else { $null })
    return [ordered]@{
        process_id = $processId
        process_name = $(if ($process) { $process.ProcessName } else { $null })
    }
}

function Get-AllowlistedProcesses($AppPolicy, [int]$SessionId) {
    $allowedNames = @($AppPolicy.process_names | ForEach-Object { ([string]$_).ToLowerInvariant() })
    return @(
        Get-Process -ErrorAction SilentlyContinue | Where-Object {
            $_.SessionId -eq $SessionId -and
            $allowedNames.Contains(([string]$_.ProcessName).ToLowerInvariant())
        }
    )
}

function Resolve-RunnerStartedTarget($AppPolicy, [int]$SessionId, [int[]]$PreExistingProcessIds, [int]$LauncherProcessId, [DateTime]$Deadline) {
    do {
        $candidates = @(
            Get-AllowlistedProcesses $AppPolicy $SessionId | Where-Object {
                $PreExistingProcessIds -notcontains $_.Id -and
                $_.MainWindowHandle -ne [IntPtr]::Zero
            }
        )
        $launcherCandidate = @($candidates | Where-Object { $_.Id -eq $LauncherProcessId }) | Select-Object -First 1
        if ($launcherCandidate) { return $launcherCandidate }
        if ($candidates.Count -eq 1) { return $candidates[0] }
        if ($candidates.Count -gt 1) { throw 'Launch produced multiple allowlisted interactive targets' }
        Start-Sleep -Milliseconds 200
    } while ((Get-Date) -lt $Deadline)
    throw 'Runner-started allowlisted target has no interactive window'
}

function Assert-TargetForeground($TargetProcess, $AppPolicy) {
    if (-not $TargetProcess) { throw 'Target app has not been launched' }
    $TargetProcess.Refresh()
    if ($TargetProcess.HasExited) { throw 'Target app exited before input' }
    if ($TargetProcess.MainWindowHandle -eq [IntPtr]::Zero) {
        throw 'Target app has no interactive window'
    }
    [void][RemoteComputerUse.DesktopNative]::Activate($TargetProcess.MainWindowHandle)
    Start-Sleep -Milliseconds 150
    $foreground = Get-ForegroundProcessInfo
    $allowedNames = @($AppPolicy.process_names | ForEach-Object { ([string]$_).ToLowerInvariant() })
    if ($foreground.process_id -ne $TargetProcess.Id -or -not $allowedNames.Contains(([string]$foreground.process_name).ToLowerInvariant())) {
        throw 'Foreground window is not the runner-started allowlisted target'
    }
}

function Request-GracefulTargetExit($StartedProcess, $AppPolicy) {
    if (-not $StartedProcess) { return $true }
    $StartedProcess.Refresh()
    if ($StartedProcess.HasExited) { return $true }

    if ([string]$AppPolicy.id -eq 'chrome-agent-uat') {
        Assert-TargetForeground $StartedProcess $AppPolicy
        # Closing Chrome's last window leaves WebBridge background state dirty.
        # Chrome does not expose its toolbar through UI Automation on this VM.
        # Open the menu relative to the verified target window, then invoke
        # Chrome's X accelerator for Exit. A missed menu cannot pass because the exact
        # runner-started process group must exit before cleanup.
        $rectangle = [RemoteComputerUse.DesktopNative]::LargestVisibleWindowRectangle([uint32]$StartedProcess.Id)
        if (($rectangle[2] - $rectangle[0]) -lt 400 -or ($rectangle[3] - $rectangle[1]) -lt 200) {
            throw 'Chrome target window is too small for deterministic menu exit'
        }
        [RemoteComputerUse.DesktopNative]::MovePointer(($rectangle[2] - 24), ($rectangle[1] + 63))
        [RemoteComputerUse.DesktopNative]::Click('left', 1)
        Start-Sleep -Milliseconds 500
        # Input is global at the Win32 layer. Re-acquire and verify the exact
        # runner-started target after the menu animation before sending a key.
        Assert-TargetForeground $StartedProcess $AppPolicy
        [RemoteComputerUse.DesktopNative]::KeyDown([uint16]0x58)
        [RemoteComputerUse.DesktopNative]::KeyUp([uint16]0x58)
        return $true
    }

    return [bool]$StartedProcess.CloseMainWindow()
}

function Stop-StartedTarget($StartedProcess) {
    if (-not $StartedProcess) { return }
    $StartedProcess.Refresh()
    if ($StartedProcess.HasExited) { return }
    [void]$StartedProcess.CloseMainWindow()
    if (-not $StartedProcess.WaitForExit(3000)) {
        $StartedProcess.Kill()
        [void]$StartedProcess.WaitForExit(3000)
    }
}

function Wait-RunnerStartedAppExit($AppPolicy, [int]$SessionId, [int[]]$PreExistingProcessIds, [int]$TimeoutMilliseconds) {
    $deadline = (Get-Date).AddMilliseconds($TimeoutMilliseconds)
    do {
        $remaining = @(
            Get-AllowlistedProcesses $AppPolicy $SessionId | Where-Object {
                $PreExistingProcessIds -notcontains $_.Id
            }
        )
        if ($remaining.Count -eq 0) { return $true }
        Start-Sleep -Milliseconds 200
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Send-DesktopKey([string]$Key, [object[]]$Modifiers) {
    $virtualKeys = @{
        CTRL = 0x11; SHIFT = 0x10; ALT = 0x12; TAB = 0x09; ENTER = 0x0D;
        ESCAPE = 0x1B; BACKSPACE = 0x08; DELETE = 0x2E; SPACE = 0x20;
        HOME = 0x24; END = 0x23; PAGEUP = 0x21; PAGEDOWN = 0x22;
        ARROWUP = 0x26; ARROWDOWN = 0x28; ARROWLEFT = 0x25; ARROWRIGHT = 0x27; F5 = 0x74
    }
    foreach ($modifier in $Modifiers) {
        [RemoteComputerUse.DesktopNative]::KeyDown([uint16]$virtualKeys[[string]$modifier])
    }
    try {
        [RemoteComputerUse.DesktopNative]::KeyDown([uint16]$virtualKeys[$Key])
        [RemoteComputerUse.DesktopNative]::KeyUp([uint16]$virtualKeys[$Key])
    }
    finally {
        for ($index = $Modifiers.Count - 1; $index -ge 0; $index--) {
            [RemoteComputerUse.DesktopNative]::KeyUp([uint16]$virtualKeys[[string]$Modifiers[$index]])
        }
    }
}

function Write-RuntimeMetadata([string]$ArtifactRoot, [string]$AppId, [int]$LauncherProcessId, [int]$AppProcessId) {
    Write-JsonAtomic (Join-Path $ArtifactRoot 'desktop-runtime.json') ([ordered]@{
        schema_version = 2
        broker_process_id = $PID
        session_id = [System.Diagnostics.Process]::GetCurrentProcess().SessionId
        app_id = $AppId
        launcher_process_id = $LauncherProcessId
        app_process_id = $AppProcessId
        updated_at = [DateTime]::UtcNow.ToString('o')
    })
}

$artifactRoot = $null
$stdoutPath = $null
$stderrPath = $null
$resultPath = $null
$contract = $null
$startedProcess = $null
$targetProcess = $null
$appPolicy = $null
$sessionId = 0
$preExistingProcessIds = @(0)
$jobHandle = [IntPtr]::Zero
$actionResults = New-Object System.Collections.ArrayList
$startedAt = Get-Date
$status = 'FAIL'
$errorMessage = $null

try {
    if (-not (Test-Path -LiteralPath $RequestPath -PathType Leaf)) {
        throw 'Desktop request is missing'
    }
    $request = Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $contractPath = [string]$request.contract_path
    $configPath = [string]$request.config_path
    $artifactRoot = [string]$request.artifact_dir
    if (-not (Test-PathUnderRoot $contractPath (Join-Path $Root 'state\browser.lease'))) {
        throw 'Desktop contract escaped the active lease'
    }
    if ([System.IO.Path]::GetFullPath($configPath) -ne [System.IO.Path]::GetFullPath((Join-Path $Root 'config.json'))) {
        throw 'Desktop config path is not canonical'
    }
    if (-not (Test-PathUnderRoot $artifactRoot (Join-Path $Root 'artifacts'))) {
        throw 'Desktop artifact path escaped the artifact root'
    }
    New-Item -ItemType Directory -Force -Path $artifactRoot | Out-Null
    $stdoutPath = Join-Path $artifactRoot 'runner.stdout.log'
    $stderrPath = Join-Path $artifactRoot 'runner.stderr.log'
    $resultPath = Join-Path $artifactRoot 'result.json'
    Write-Utf8NoBom $stdoutPath "desktop broker started`n"
    Write-Utf8NoBom $stderrPath ''

    $contract = Get-Content -LiteralPath $contractPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $node = Get-Command node.exe -ErrorAction Stop
    $validatorPath = Join-Path $CurrentRoot 'src\validate-contract.mjs'
    $validationOutput = & $node.Source $validatorPath $contractPath $configPath 2>$null
    $validationExit = $LASTEXITCODE
    $validation = $validationOutput | Select-Object -Last 1 | ConvertFrom-Json
    if ($validationExit -ne 0 -or $validation.status -ne 'VALID' -or $validation.mode -ne 'desktop') {
        throw "Desktop request failed live policy validation: $([string]$validation.error)"
    }
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    if ($identity -ne [string]$config.desktop_user) {
        throw 'Desktop broker is running as an unexpected Windows identity'
    }

    Initialize-DesktopNativeType
    [RemoteComputerUse.DesktopNative]::EnableDpiAwareness()
    $sessionId = [System.Diagnostics.Process]::GetCurrentProcess().SessionId
    $activeConsoleSessionId = [int][RemoteComputerUse.DesktopNative]::ActiveConsoleSessionId()
    if ($sessionId -le 0 -or $sessionId -ne $activeConsoleSessionId) {
        throw 'Desktop broker is not running in the active console session'
    }
    Write-RuntimeMetadata $artifactRoot $null 0 0

    $beforeBounds = Save-DesktopScreenshot (Join-Path $artifactRoot 'before.png')
    $desktopMetadata = [ordered]@{
        schema_version = 2
        session_id = $sessionId
        active_console_session_id = $activeConsoleSessionId
        identity = $identity
        virtual_screen = [ordered]@{
            left = $beforeBounds.Left
            top = $beforeBounds.Top
            width = $beforeBounds.Width
            height = $beforeBounds.Height
        }
        foreground_before = Get-ForegroundProcessInfo
    }
    Write-JsonAtomic (Join-Path $artifactRoot 'desktop.json') $desktopMetadata

    $appPolicy = $(if ($contract.capability -eq 'uat') { Get-AppPolicy $config ([string]$contract.target.app_id) } else { $null })
    foreach ($action in @($contract.actions)) {
        if (Test-Path -LiteralPath (Join-Path (Split-Path -Parent $RequestPath) 'stop.requested') -PathType Leaf) {
            throw 'Desktop stop was requested by the Session 0 controller'
        }
        $entry = [ordered]@{ type = [string]$action.type; status = 'PASS' }
        switch ([string]$action.type) {
            'screenshot' {
                [void](Save-DesktopScreenshot (Join-Path $artifactRoot (([string]$action.name) + '.png')))
                $entry.name = [string]$action.name
            }
            'wait' {
                Start-Sleep -Milliseconds ([int]$action.milliseconds)
                $entry.milliseconds = [int]$action.milliseconds
            }
            'launch' {
                if (-not $appPolicy -or $action.app_id -ne $appPolicy.id) { throw 'Launch target is not allowlisted' }
                $preExistingProcesses = @(Get-AllowlistedProcesses $appPolicy $sessionId)
                if (@($preExistingProcesses | Where-Object { $_.MainWindowHandle -ne [IntPtr]::Zero }).Count -gt 0) {
                    throw 'An allowlisted target window already exists before launch'
                }
                $preExistingProcessIds = @(0) + @($preExistingProcesses | ForEach-Object { [int]$_.Id })
                $arguments = @($appPolicy.arguments | ForEach-Object { [string]$_ })
                $startArguments = @{
                    FilePath = [string]$appPolicy.executable
                    PassThru = $true
                }
                if ($arguments.Count -gt 0) { $startArguments.ArgumentList = $arguments }
                $startedProcess = Start-Process @startArguments
                $jobHandle = [RemoteComputerUse.DesktopNative]::CreateKillOnCloseJob()
                [RemoteComputerUse.DesktopNative]::AssignToJob($jobHandle, $startedProcess.Handle)
                $deadline = (Get-Date).AddSeconds(8)
                $targetProcess = Resolve-RunnerStartedTarget $appPolicy $sessionId $preExistingProcessIds $startedProcess.Id $deadline
                Write-RuntimeMetadata $artifactRoot ([string]$action.app_id) $startedProcess.Id $targetProcess.Id
                Assert-TargetForeground $targetProcess $appPolicy
                $entry.app_id = [string]$action.app_id
                $entry.launcher_process_id = $startedProcess.Id
                $entry.process_id = $targetProcess.Id
            }
            'move' {
                Assert-TargetForeground $targetProcess $appPolicy
                $bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
                if ($action.x -lt $bounds.Left -or $action.x -ge $bounds.Right -or $action.y -lt $bounds.Top -or $action.y -ge $bounds.Bottom) {
                    throw 'Pointer coordinates are outside the virtual screen'
                }
                [RemoteComputerUse.DesktopNative]::MovePointer([int]$action.x, [int]$action.y)
                $entry.x = [int]$action.x
                $entry.y = [int]$action.y
            }
            'click' {
                Assert-TargetForeground $targetProcess $appPolicy
                $bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
                if ($action.x -lt $bounds.Left -or $action.x -ge $bounds.Right -or $action.y -lt $bounds.Top -or $action.y -ge $bounds.Bottom) {
                    throw 'Click coordinates are outside the virtual screen'
                }
                [RemoteComputerUse.DesktopNative]::MovePointer([int]$action.x, [int]$action.y)
                [RemoteComputerUse.DesktopNative]::Click([string]$action.button, [int]$action.count)
                $entry.x = [int]$action.x
                $entry.y = [int]$action.y
                $entry.button = [string]$action.button
                $entry.count = [int]$action.count
            }
            'text' {
                Assert-TargetForeground $targetProcess $appPolicy
                $entry.characters = [RemoteComputerUse.DesktopNative]::SendText([string]$action.value)
                $entry.value_logged = $false
            }
            'key' {
                Assert-TargetForeground $targetProcess $appPolicy
                Send-DesktopKey ([string]$action.key) @($action.modifiers)
                $entry.key = [string]$action.key
                $entry.modifiers = @($action.modifiers)
            }
            'close_target' {
                if (-not (Request-GracefulTargetExit $targetProcess $appPolicy)) {
                    throw 'Allowlisted target refused a graceful exit request'
                }
                if (-not (Wait-RunnerStartedAppExit $appPolicy $sessionId $preExistingProcessIds 8000)) {
                    throw 'Runner-started allowlisted process group did not exit gracefully'
                }
                $targetProcess = $null
                $entry.started_process_only = $true
                $entry.graceful_group_exit = $true
            }
            default { throw 'Unsupported desktop action reached the broker' }
        }
        [void]$actionResults.Add($entry)
    }
    [void](Save-DesktopScreenshot (Join-Path $artifactRoot 'final.png'))
    $status = 'PASS'
}
catch {
    $errorMessage = $_.Exception.Message.Substring(0, [Math]::Min(1000, $_.Exception.Message.Length))
    if ($stderrPath) { Write-Utf8NoBom $stderrPath ($errorMessage + "`n") }
    if ($artifactRoot -and (Test-Path -LiteralPath $artifactRoot -PathType Container)) {
        try { [void](Save-DesktopScreenshot (Join-Path $artifactRoot 'failure.png')) } catch { }
    }
}
finally {
    try {
        Stop-StartedTarget $targetProcess
        if ($appPolicy -and $sessionId -gt 0) {
            [void](Wait-RunnerStartedAppExit $appPolicy $sessionId $preExistingProcessIds 3000)
        }
    }
    catch { }
    if ($jobHandle -ne [IntPtr]::Zero) {
        [RemoteComputerUse.DesktopNative]::CloseJob($jobHandle)
        $jobHandle = [IntPtr]::Zero
    }
    if ($artifactRoot -and (Test-Path -LiteralPath $artifactRoot -PathType Container)) {
        Write-JsonAtomic (Join-Path $artifactRoot 'action-ledger.json') @($actionResults)
        if (-not $resultPath) { $resultPath = Join-Path $artifactRoot 'result.json' }
        $finishedAt = Get-Date
        $result = [ordered]@{
            schema_version = 2
            runner_version = $RunnerVersion
            task_id = $(if ($contract) { [string]$contract.task_id } else { $null })
            mode = 'desktop'
            capability = $(if ($contract) { [string]$contract.capability } else { $null })
            status = $status
            error = $errorMessage
            started_at = $startedAt.ToUniversalTime().ToString('o')
            finished_at = $finishedAt.ToUniversalTime().ToString('o')
            duration_seconds = [Math]::Round(($finishedAt - $startedAt).TotalSeconds, 3)
            target = $(if ($contract) { $contract.target } else { $null })
            side_effects = $(if ($contract) { [string]$contract.side_effects } else { $null })
            actions = @($actionResults)
            artifact_dir = $artifactRoot
            session_id = [System.Diagnostics.Process]::GetCurrentProcess().SessionId
        }
        Write-JsonAtomic $resultPath $result
        if ($stdoutPath) { Add-Content -LiteralPath $stdoutPath -Value ("desktop broker completed: " + $status) -Encoding UTF8 }
    }
}

exit $(if ($status -eq 'PASS') { 0 } else { 1 })
