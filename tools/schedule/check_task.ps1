# -*- coding: utf-8 -*-
# 只读体检：计划任务 QuantPanorama_DailyRun 是否还在、配置是否还正确、有没有真跑过
#
# ★ 本脚本不做任何修改（不注册、不删除、不启动），随时可跑。
# ★ 判定以磁盘定义为准（C:\Windows\System32\Tasks\<任务名>），不以「能查到任务名」为准。
#
# 退出码：0 = 任务在册且关键配置正确；1 = 有问题（缺失 / 配置漂移 / 读写异常）
param(
    [string]$TaskName = 'QuantPanorama_DailyRun'
)

$ErrorActionPreference = 'Continue'

$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Bat  = Join-Path $Root 'daily_run.bat'
. (Join-Path $PSScriptRoot 'task_action.ps1')

$problems = @()
$notes    = @()

Write-Output '=============================================================='
Write-Output ('  计划任务只读体检  ' + (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))
Write-Output ('  任务: ' + $TaskName)
Write-Output '=============================================================='

# --- 1. 磁盘定义（权威） --------------------------------------------------
$defPath = 'C:\Windows\System32\Tasks\' + $TaskName
$defXml  = ''
if (Test-Path -LiteralPath $defPath) {
    try {
        $defXml = [System.IO.File]::ReadAllText($defPath, [System.Text.Encoding]::Unicode)
        $notes += ('磁盘定义存在: ' + $defPath + ' (' + (Get-Item -LiteralPath $defPath).Length + ' 字节)')
    } catch {
        $problems += ('磁盘定义读不到: ' + $_.Exception.Message)
    }
} else {
    # 目录 ACL 可能不允许 stat；此时用 Test-Path 也可能为假 -> 交叉验证服务端视图
    $problems += '无法读取磁盘定义，不能证明任务已落盘；服务端视图仅用于补充诊断'
}

# --- 2. 服务端视图 --------------------------------------------------------
$svcFound = $false
try {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $svcFound = $true
    $i = Get-ScheduledTaskInfo -TaskName $TaskName
    $notes += ('服务端视图: State=' + $t.State + '  NextRunTime=' + $i.NextRunTime +
               '  LastRunTime=' + $i.LastRunTime + '  LastTaskResult=' + $i.LastTaskResult +
               '  MissedRuns=' + $i.NumberOfMissedRuns)
    $act = ''
    if ($t.Actions.Count -gt 0) { $act = [string]$t.Actions[0].Execute + ' ' + [string]$t.Actions[0].Arguments }
    $notes += ('动作: ' + $act)
    if ($t.Actions.Count -ne 1) {
        $problems += '任务必须只有一个已核验动作'
    } else {
        $problems += @(Test-QuantTaskAction -Root $Root -Action $t.Actions[0])
    }
    if ($t.State -eq 'Disabled') { $problems += '任务已被禁用 (Disabled)' }
} catch {
    $problems += ('服务端查不到该任务: ' + $_.Exception.Message)
}

if (-not $svcFound -and $defXml -eq '') {
    $problems += '任务既不在服务端也不在磁盘 -> 任务已被删除，自动交易处于停摆状态'
}

# --- 3. 关键配置漂移（仅有磁盘定义时逐项比对） ----------------------------
if ($defXml -ne '') {
    try {
        $disk = [xml]$defXml
        $exec = @($disk.Task.Actions.Exec)
        if ($exec.Count -ne 1) {
            $problems += '磁盘定义必须只有一个执行动作'
        } else {
            $diskAction = @{ Execute = [string]$exec[0].Command; Arguments = [string]$exec[0].Arguments; WorkingDirectory = [string]$exec[0].WorkingDirectory }
            $problems += @(Test-QuantTaskAction -Root $Root -Action $diskAction)
        }
    } catch { $problems += ('磁盘动作解析失败: ' + $_.Exception.Message) }

    $checks = @(
        @{ re = '<StartWhenAvailable>true';          why = 'StartWhenAvailable != true（错过 14:50 不会补跑）' },
        @{ re = '<DisallowStartIfOnBatteries>false'; why = 'DisallowStartIfOnBatteries != false（电池下不跑）' },
        @{ re = '<StopIfGoingOnBatteries>false';     why = 'StopIfGoingOnBatteries != false（切电池被打断）' },
        @{ re = '<RunOnlyIfIdle>false';              why = 'RunOnlyIfIdle != false（要等空闲才跑）' },
        @{ re = '<LogonType>InteractiveToken';       why = 'LogonType != InteractiveToken（GUI 自动化找不到同花顺窗口）' }
    )
    foreach ($c in $checks) {
        if ($defXml -notmatch $c.re) { $problems += $c.why }
    }
    foreach ($d in @('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday')) {
        if ($defXml -notmatch ('<' + $d)) { $problems += ('触发器缺少 ' + $d) }
    }
    if ($defXml -match '<StartBoundary>([^<]+)</StartBoundary>') {
        $notes += ('StartBoundary = ' + $Matches[1])
    }
}

# --- 4. 最近一次运行记录（应用侧证据） ------------------------------------
$statusPath = Join-Path $Root 'state\last_run.json'
if (Test-Path -LiteralPath $statusPath) {
    try {
        # 必须显式 -Encoding UTF8：PS5.1 对无 BOM 的 JSON 默认按 ANSI(GBK) 读 -> 中文乱码
        $st = Get-Content -LiteralPath $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $notes += ('最近一次运行: ' + $st.ts + '  exit=' + $st.exit_code + '  ok=' + $st.ok +
                   '  ' + [string]$st.summary)
        if (-not $st.ok) { $problems += '最近一次运行是失败的（见 state/ALERT.txt）' }
        $age = (Get-Date) - [datetime]$st.ts
        if ($age.TotalDays -gt 5) {
            $problems += ('最近一次运行距今已 ' + [math]::Round($age.TotalDays, 1) + ' 天（含节假日正常跳过，但仍需人工确认）')
        }
    } catch {
        $problems += ('state/last_run.json 解析失败: ' + $_.Exception.Message)
    }
} else {
    $problems += 'state/last_run.json 不存在 -> 从未有过计划运行记录'
}
foreach ($f in @('ALERT.txt', 'DATA_ALERT.txt')) {
    $p = Join-Path $Root ('state\' + $f)
    if (Test-Path -LiteralPath $p) { $problems += ('存在告警文件 state\' + $f) }
}

# --- 输出 -----------------------------------------------------------------
Write-Output ''
foreach ($n in $notes) { Write-Output ('  . ' + $n) }
Write-Output ''
if ($problems.Count -gt 0) {
    Write-Output '[有问题]'
    foreach ($p in $problems) { Write-Output ('  x ' + $p) }
    Write-Output ''
    Write-Output '修复入口: 双击 install_schedule.bat （会自动提权重注册并核对磁盘定义）'
    exit 1
}
Write-Output '[正常] 任务在册、关键配置正确、最近一次运行无异常。'
Write-Output '       [提醒] 「在册」只代表此刻。任务曾在本机无声消失过，建议每个交易日跑一次本检查。'
exit 0
