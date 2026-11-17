# Preserve the real batch exit code while keeping the helper console hidden.
param(
    [string]$CommandExecutable = $env:ComSpec,
    [string]$CommandArguments = '',
    [string]$WorkingDirectory = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
try {
    if ([string]::IsNullOrWhiteSpace($CommandExecutable)) {
        $CommandExecutable = 'cmd.exe'
    }
    if ([string]::IsNullOrWhiteSpace($CommandArguments)) {
        $batch = Join-Path $WorkingDirectory 'daily_run.bat'
        if (-not (Test-Path -LiteralPath $batch -PathType Leaf)) {
            throw ('Missing batch: ' + $batch)
        }
        $CommandArguments = '/d /s /c ""' + $batch + '""'
    }
    $child = Start-Process -FilePath $CommandExecutable `
        -ArgumentList $CommandArguments -WorkingDirectory $WorkingDirectory `
        -WindowStyle Hidden -PassThru -Wait
    exit $child.ExitCode
} catch {
    Write-Error $_
    exit 1
}
