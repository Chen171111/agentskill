# -*- coding: utf-8 -*-
# 注册 / 修复「每日自动交易」计划任务 QuantPanorama_DailyRun
#
# 为什么用 Register-ScheduledTask 而不是 schtasks /create：
#   本机 2026-09-24 实测：普通权限下 `schtasks /create`（内联与 /xml 两种形式）
#   均报 "ERROR: Access is denied."（rc=1）；且更早出现过「rc=0 + /query 能查到，
#   但配置根本没落盘」的假阳性。Register-ScheduledTask 在本机普通权限下注册成功
#   （2026-09-30 实测），且能直读 UTF-16 的 xml。
#
# 判定标准（本文件的核心）：**以磁盘定义为准**，不看退出码、不看「能查到任务名」。
#   读 C:\Windows\System32\Tasks\<任务名>（UTF-16）逐项比对关键设置与动作路径。
#
# 退出码：0 = 已注册且磁盘定义核对通过；1 = 失败（详见输出）
param(
    [string]$TaskName = 'QuantPanorama_DailyRun'
)

$ErrorActionPreference = 'Stop'

# 仓库根目录 = 本脚本所在目录的上一级
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Bat  = Join-Path $Root 'daily_run.bat'
$Exe  = 'cmd.exe'
$Args = '/c "' + $Bat + '"'

function Say { Write-Output ($args -join '') }

Say '=============================================================='
Say ('  注册 / 修复计划任务: ' + $TaskName)
Say ('  仓库: ' + $Root)
Say '=============================================================='

if (-not (Test-Path -LiteralPath $Bat)) {
    Say ('[FAIL] 找不到 daily_run.bat: ' + $Bat)
    exit 1
}

# --- 起始时间：下一个「工作日 14:50」，且至少留 10 分钟余量 --------------
# 不把 StartBoundary 设成「今天 14:50」：该时点可能已过，配合
# StartWhenAvailable=true 会让任务在注册后立刻补跑一次（收盘后下单风险）。
$now   = Get-Date
$start = $now.Date.AddHours(14).AddMinutes(50)
if ($start -le $now.AddMinutes(10)) { $start = $start.AddDays(1) }
while ($start.DayOfWeek -eq 'Saturday' -or $start.DayOfWeek -eq 'Sunday') {
    $start = $start.AddDays(1)
}
Say ('起始时间 (StartBoundary): ' + $start.ToString('yyyy-MM-dd HH:mm') + ' ' + $start.DayOfWeek)

# --- 构造任务：工作日 14:50，仅登录时运行（同花顺需要交互桌面） ----------
$action = New-ScheduledTaskAction -Execute $Exe -Argument $Args -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $start
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)
$principal = New-ScheduledTaskPrincipal -UserId ($env:USERDOMAIN + '\' + $env:USERNAME) `
    -LogonType Interactive -RunLevel Limited
$desc = 'Daily auto-trade (refresh data + one THS session), Mon-Fri 14:50 local.'

# --- 注册（-Force = 覆盖同名旧定义） --------------------------------------
$regErr = ''
try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Description $desc -Force | Out-Null
    Say '[i] Register-ScheduledTask 返回成功（尚不能据此判定，继续核对磁盘定义）'
} catch {
    $regErr = $_.Exception.Message
    Say ('[FAIL] 注册被拒绝: ' + $regErr)
    Say '       若为 Access is denied -> 用 install_schedule.bat 走提权重试，'
    Say ('       或用任务计划程序 GUI: 操作 -> 导入任务 -> tools\schedule\' + $TaskName + '.xml')
    exit 1
}

# --- 核对 1：磁盘任务定义（权威） ----------------------------------------
Say ''
Say ('--- 核对：磁盘任务定义 (C:\Windows\System32\Tasks\' + $TaskName + ') ---')
$defPath = 'C:\Windows\System32\Tasks\' + $TaskName
$defXml  = ''
try {
    $defXml = [System.IO.File]::ReadAllText($defPath, [System.Text.Encoding]::Unicode)
    Say ('[i] 定义文件存在，' + (Get-Item -LiteralPath $defPath).Length + ' 字节')
} catch {
    Say ('[FAIL] 读不到磁盘定义: ' + $_.Exception.Message)
    Say '       注意：读不到 = 无法证明已落盘。不要把它当成成功。'
    exit 1
}

$fail = @()
if ($defXml -notmatch [regex]::Escape($Bat))            { $fail += '动作路径里没有 daily_run.bat' }
if ($defXml -notmatch [regex]::Escape($Root))           { $fail += 'WorkingDirectory 不是本仓库' }
if ($defXml -notmatch '<StartWhenAvailable>true')       { $fail += 'StartWhenAvailable != true（错过时点不会补跑）' }
if ($defXml -notmatch '<DisallowStartIfOnBatteries>false') { $fail += 'DisallowStartIfOnBatteries != false（笔记本电池下不跑）' }
if ($defXml -notmatch '<StopIfGoingOnBatteries>false')  { $fail += 'StopIfGoingOnBatteries != false（切电池会被打断）' }
if ($defXml -notmatch '<RunOnlyIfIdle>false')           { $fail += 'RunOnlyIfIdle != false（会等到空闲才跑）' }
if ($defXml -notmatch '<LogonType>InteractiveToken')    { $fail += 'LogonType != InteractiveToken（GUI 自动化会找不到同花顺窗口）' }
foreach ($d in @('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday')) {
    if ($defXml -notmatch ('<' + $d)) { $fail += ('触发器缺少 ' + $d) }
}
if ($defXml -match '<StartBoundary>([^<]+)</StartBoundary>') {
    Say ('[i] StartBoundary = ' + $Matches[1])
} else {
    $fail += '找不到 StartBoundary'
}

# --- 核对 2：服务端视图 + 下次运行时间 ------------------------------------
$svcOk = $true
try {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $i = Get-ScheduledTaskInfo -TaskName $TaskName
    Say ('[i] State = ' + $t.State + ' / NextRunTime = ' + $i.NextRunTime + ' / NumberOfMissedRuns = ' + $i.NumberOfMissedRuns)
    if ($t.Actions[0].Arguments -notmatch [regex]::Escape($Bat)) { $fail += '服务端动作路径不符' }
    if ($null -eq $i.NextRunTime) { $fail += 'NextRunTime 为空（触发器没生效）' }
} catch {
    Say ('[FAIL] 服务端查询失败: ' + $_.Exception.Message)
    $svcOk = $false
}

Say ''
if ($fail.Count -gt 0) {
    Say '[FAIL] 磁盘定义核对未通过：'
    foreach ($f in $fail) { Say ('   x ' + $f) }
    Say '       -> 不要认为任务已生效。请用 install_schedule.bat 提权重试，'
    Say ('          或任务计划程序 GUI 导入 tools\schedule\' + $TaskName + '.xml')
    exit 1
}
if (-not $svcOk) { exit 1 }

Say '[OK] 已注册，且磁盘定义核对通过。'
Say ('     下次运行 = ' + (Get-ScheduledTaskInfo -TaskName $TaskName).NextRunTime)
Say '     [提醒] 本机教训：注册成功的任务仍可能事后消失，'
Say '            请隔一个周期用 check_schedule.bat 复查一次它是否真跑过。'
exit 0
