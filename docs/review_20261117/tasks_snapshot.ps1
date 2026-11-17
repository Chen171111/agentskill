# Read-only Task Scheduler inventory; never register, enable, or start a task.
$ErrorActionPreference = 'Stop'
$auditDir = $PSScriptRoot
$allTasks = @(Get-ScheduledTask)
$selected = @()
$exportErrors = @()
foreach ($task in $allTasks) {
    $actionText = (($task.Actions | ForEach-Object { $_.Execute + ' ' + $_.Arguments + ' ' + $_.WorkingDirectory }) -join ' ')
    if (($task.TaskName -notmatch 'Quant|THS') -and ($actionText -notmatch 'agentskill|ths_autostart|ths_keepvisible|ths_trade_up|daily_run\.bat|daily_job\.py')) { continue }
    try {
        $info = Get-ScheduledTaskInfo -TaskName $task.TaskName -TaskPath $task.TaskPath
        $xml = Export-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath
        $doc = [xml]$xml
        $diskPath = Join-Path 'C:\Windows\System32\Tasks' (($task.TaskPath + $task.TaskName).TrimStart('\'))
        $disk = $null
        try {
            $bytes = [System.IO.File]::ReadAllBytes($diskPath)
            $diskDoc = New-Object System.Xml.XmlDocument
            $stream = [System.IO.MemoryStream]::new($bytes, $false)
            try { $diskDoc.Load($stream) } finally { $stream.Dispose() }
            $sha = [System.Security.Cryptography.SHA256]::Create()
            try { $digest = [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '') } finally { $sha.Dispose() }
            $disk = @{ readable=$true; bytes=$bytes.Length; sha256=$digest;
                settingsXml=$diskDoc.Task.Settings.OuterXml; actionsXml=$diskDoc.Task.Actions.OuterXml;
                triggersXml=$diskDoc.Task.Triggers.OuterXml; logonType=[string]$diskDoc.Task.Principals.Principal.LogonType;
                actionsMatchExport=($diskDoc.Task.Actions.OuterXml -eq $doc.Task.Actions.OuterXml);
                settingsMatchExport=($diskDoc.Task.Settings.OuterXml -eq $doc.Task.Settings.OuterXml);
                triggersMatchExport=($diskDoc.Task.Triggers.OuterXml -eq $doc.Task.Triggers.OuterXml) }
        } catch { $disk = @{ readable=$false; error=$_.Exception.Message } }
        $selected += [ordered]@{
            name=$task.TaskPath + $task.TaskName; state=[string]$task.State
            lastRun=if ($null -ne $info.LastRunTime) { $info.LastRunTime.ToString('o') } else { $null }; lastResult=$info.LastTaskResult
            nextRun=if ($null -ne $info.NextRunTime) { $info.NextRunTime.ToString('o') } else { $null }; missedRuns=$info.NumberOfMissedRuns
            actions=@($task.Actions | ForEach-Object { @{ execute=$_.Execute; arguments=$_.Arguments; workingDirectory=$_.WorkingDirectory } })
            logonType=[string]$doc.Task.Principals.Principal.LogonType
            runLevel=[string]$doc.Task.Principals.Principal.RunLevel
            settingsXml=$doc.Task.Settings.OuterXml; triggersXml=$doc.Task.Triggers.OuterXml
            diskDefinition=$disk
        }
    } catch { $exportErrors += @{name=$task.TaskPath + $task.TaskName;error=$_.Exception.Message} }
}
$result = [ordered]@{observedAt=(Get-Date).ToString('o'); totalTasksEnumerated=$allTasks.Count; selectedCount=$selected.Count; selected=$selected; errors=$exportErrors}
$result | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $auditDir 'tasks_snapshot.json') -Encoding utf8
Write-Output ('allTasks={0}; selected={1}; errors={2}; output=tasks_snapshot.json' -f $allTasks.Count,$selected.Count,$exportErrors.Count)
