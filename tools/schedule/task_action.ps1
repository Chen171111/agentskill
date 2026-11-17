# Shared, side-effect-free action definition for registration and inspection.
function Get-QuantTaskAction {
    param([Parameter(Mandatory=$true)][string]$Root)
    $runner = Join-Path $Root 'tools\run_scheduled.ps1'
    return @{
        Execute = (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe')
        Arguments = ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $runner + '"')
        WorkingDirectory = $Root
    }
}

function Test-QuantTaskAction {
    param([Parameter(Mandatory=$true)][string]$Root, [Parameter(Mandatory=$true)]$Action)
    $expected = Get-QuantTaskAction -Root $Root
    foreach ($key in @('Execute', 'Arguments', 'WorkingDirectory')) {
        if ([string]$Action.$key -ne [string]$expected[$key]) {
            Write-Output ('Task action mismatch: ' + $key)
        }
    }
}
