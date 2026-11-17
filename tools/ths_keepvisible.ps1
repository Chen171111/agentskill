# Keep the THS trade window usable during the trading window.
#
# 2026-11-16 改版：原来只处理「最小化（IsIconic）」，**不处理「隐藏（!IsWindowVisible）」**。
# 实测 2026-11-16 14:35~15:15 这段，交易窗口一直是隐藏态 → 本脚本每轮 total>0、
# restored=0，一声不响；而 14:50 的定时任务因为只认可见窗口，直接连挂 4 次。
# 所以现在两种坏状态都处理：不可见 → SW_SHOW；最小化 → SW_RESTORE。
# 依然只在「确实不可用」时才动手，健康时静默（不刷日志、不抢焦点）。
# Scheduled 14:35 + every 10 min for 45 min, so it cannot disturb other hours.
$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $PSScriptRoot
$log = Join-Path $repo 'state\ths_keepvisible.log'

function Log([string]$m) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
    Add-Content -LiteralPath $log -Value $line -Encoding UTF8
}

Add-Type @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public class ThsRestore {
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lParam);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr hWnd, StringBuilder buf, int n);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
}
'@

$script:restored = 0
$script:total = 0
$script:acted = 0
$cb = [ThsRestore+EnumWindowsProc] {
    param($h, $l)
    $sb = New-Object System.Text.StringBuilder 512
    [void][ThsRestore]::GetWindowTextW($h, $sb, 512)
    $t = $sb.ToString()
    if ($t -match '股票交易系统') {
        $script:total++
        # A window can be hidden AND minimized. Restore takes precedence;
        # SW_SHOW alone leaves that combination minimized.
        if ([ThsRestore]::IsIconic($h)) {
            [void][ThsRestore]::ShowWindow($h, 9)
            $script:restored++
            Log ("restored minimized window: " + $t)
        } elseif (-not [ThsRestore]::IsWindowVisible($h)) {
            [void][ThsRestore]::ShowWindow($h, 5)
            $script:restored++
            Log ("shown hidden window: " + $t)
        }
    }
    return $true
}
[void][ThsRestore]::EnumWindows($cb, [IntPtr]::Zero)

if ($script:total -eq 0) {
    Log 'trade window: NOT FOUND (client not running)'
} elseif ($script:restored -eq 0) {
    # silent when everything is fine - only note absence every run is too noisy, so skip
}
