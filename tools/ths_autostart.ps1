# THS auto-start (login trigger): 优先把「模拟交易盘」拉到可交易状态，再起行情端。
#
# 2026-11-16 改版背景：
#   旧版只按「进程在不在」判断，进程起来就算完事 —— 但同花顺开机自启后
#   交易窗口 `网上股票交易系统5.0` 可能是**隐藏**的（IsWindowVisible=False），
#   进程在、窗口不在明面上，14:50 的定时任务照样连不上（当日实录）。
#   所以现在以「交易窗口是否可见且交易控件齐全」为唯一成功判据，
#   并调用 tools/ths_trade_up.py 做显示/还原（同一套逻辑，避免两处实现漂移）。
#
# 顺序：xiadan（模拟交易盘）→ 就绪检查 → hexin（行情）→ 再看一次交易窗口。
# 参数：-EnsureWaitSec 交易窗口等待秒数（开机登录慢，给足 180 秒）。
param(
    [string]$HexinExe = 'D:\同花顺软件\同花顺\hexin.exe',
    [string]$TradeExe = 'D:\同花顺软件\同花顺\xiadan.exe',
    [int]$EnsureWaitSec = 180,
    [switch]$TradeOnly,
    [string]$Python32 = $env:PYTHON32
)

$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $PSScriptRoot
$log = Join-Path $repo 'state\ths_autostart.log'
$tradeUp = Join-Path $PSScriptRoot 'ths_trade_up.py'
$py32 = $Python32
if ([string]::IsNullOrWhiteSpace($py32)) { $py32 = 'E:\Python32\python.exe' }
[void][System.IO.Directory]::CreateDirectory((Split-Path -Parent $log))

function Log([string]$m) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
    Add-Content -LiteralPath $log -Value $line -Encoding UTF8
}

# 调 ths_trade_up.py 做「显示 + 就绪判定」，返回其退出码（0 = 就绪）
function Ensure-TradeWindow([int]$sec, [string]$tag) {
    if (-not (Test-Path -LiteralPath $py32)) {
        Log ($tag + ' MISSING python32: ' + $py32)
        return 9
    }
    if (-not (Test-Path -LiteralPath $tradeUp)) {
        Log ($tag + ' MISSING ths_trade_up.py: ' + $tradeUp)
        return 9
    }
    # ⚠️ 必须显式把控制台解码切到 UTF-8：python 输出的是 UTF-8，PS 5.1 默认按
    # 本地代码页(GBK)解码 → 中文全成「鏁?锛?」这类乱码写进 UTF-8 日志。
    $oldEnc = [Console]::OutputEncoding
    $oldPy = $env:PYTHONIOENCODING
    try {
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
        $env:PYTHONIOENCODING = 'utf-8'
        $out = & $py32 -X utf8 $tradeUp --ensure --wait $sec 2>&1
        $rc = $LASTEXITCODE
    } finally {
        [Console]::OutputEncoding = $oldEnc
        if ($null -eq $oldPy) {
            Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue
        } else {
            $env:PYTHONIOENCODING = $oldPy
        }
    }
    foreach ($l in $out) {
        if ("$l" -match '^(state|detail|hwnd|procs|window|建议|已执行)') { Log ($tag + ' ' + "$l") }
    }
    Log ($tag + ' ensure rc=' + $rc + ' wait=' + $sec)
    return $rc
}

Log '--- autostart begin (trade-first) ---'

# ── 1. 模拟交易盘优先 ──────────────────────────────────────────────
$trade = @(Get-Process -Name xiadan -ErrorAction SilentlyContinue)
if ($trade.Count -gt 0) {
    Log ("xiadan already running pid=" + (($trade | ForEach-Object { $_.Id }) -join ','))
} elseif (Test-Path -LiteralPath $TradeExe) {
    Start-Process -FilePath $TradeExe -WorkingDirectory (Split-Path -Parent $TradeExe) -WindowStyle Hidden | Out-Null
    Log 'started xiadan (trade client first)'
    Start-Sleep -Seconds 8
} else {
    Log ("MISSING xiadan: " + $TradeExe)
}

$tradeRc = Ensure-TradeWindow $EnsureWaitSec 'trade:'

# ── 2. 行情端（不阻塞交易，放在交易盘之后）────────────────────────
if (-not $TradeOnly) {
$hexin = @(Get-Process -Name hexin -ErrorAction SilentlyContinue)
if ($hexin.Count -gt 0) {
    Log ("hexin already running pid=" + (($hexin | ForEach-Object { $_.Id }) -join ','))
} elseif (Test-Path -LiteralPath $HexinExe) {
    Start-Process -FilePath $HexinExe -WorkingDirectory (Split-Path -Parent $HexinExe) -WindowStyle Hidden | Out-Null
    Log 'started hexin'
    Start-Sleep -Seconds 25
} else {
    Log ("MISSING hexin: " + $HexinExe)
}

}

# ── 3. 行情起来后复查一次（hexin 可能带动登录/重绘交易窗口）──────
if ($tradeRc -ne 0 -and -not $TradeOnly) {
    Log 'trade: retry after hexin'
    $tradeRc = Ensure-TradeWindow 60 'trade2:'
}

# ── 4. 留档：窗口真实状态（不看可见性，隐藏也要记下来）───────────
Add-Type @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public class ThsWin {
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lParam);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr hWnd, StringBuilder buf, int n);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
}
'@

$script:hits = @()
$cb = [ThsWin+EnumWindowsProc] {
    param($h, $l)
    $sb = New-Object System.Text.StringBuilder 512
    [void][ThsWin]::GetWindowTextW($h, $sb, 512)
    $t = $sb.ToString()
    if ($t -match '股票交易系统') {
        $script:hits += ("{0} | visible={1} iconic={2}" -f $t, [ThsWin]::IsWindowVisible($h), [ThsWin]::IsIconic($h))
    }
    return $true
}
[void][ThsWin]::EnumWindows($cb, [IntPtr]::Zero)

if ($script:hits.Count -eq 0) {
    Log 'trade window: NOT FOUND'
} else {
    foreach ($h in $script:hits) { Log ("trade window: " + $h) }
}

if ($tradeRc -ne 0) {
    Log ('WARN 模拟交易盘未就绪 rc=' + $tradeRc + ' —— 需人工登录/检查（本脚本不代登录）')
} else {
    Log 'trade: READY'
}
Log '--- autostart end ---'
exit $tradeRc
