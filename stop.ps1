param([switch]$StopInfrastructure)
$ErrorActionPreference='Stop'
$fusionRoot=[IO.Path]::GetFullPath($PSScriptRoot)
. (Join-Path $fusionRoot 'scripts\managed-processes.ps1')
$fusionPidPath=Join-Path $fusionRoot '.runtime\pids.json'
$fusionVoicePidPath=Join-Path $fusionRoot '.runtime\voice-validation-pids.json'
$fusionVoiceRecords=@(Read-FusionProcessRecords $fusionVoicePidPath)
$fusionCloudFailed=$false
if ($fusionVoiceRecords.Count) {
    & (Join-Path $fusionRoot 'backend\.venv\Scripts\python.exe') (Join-Path $fusionRoot 'scripts\stop_voice_tasks.py')
    if ($LASTEXITCODE -ne 0) {
        $fusionCloudFailed=$true
        Write-Warning '云端任务清理未完成。请重试 stop-voice.cmd 或在 RTC 控制台核对；仍会关闭本地进程。'
    }
}
$fusionOwned=@{}
foreach ($fusionManagedPath in @($fusionPidPath,$fusionVoicePidPath)) {
    foreach ($fusionEntry in @(Read-FusionProcessRecords $fusionManagedPath)) {
        $fusionProcess=Get-CimInstance Win32_Process -Filter "ProcessId=$($fusionEntry.id)" -ErrorAction SilentlyContinue
        if (Test-FusionOwnedProcess $fusionProcess $fusionRoot) { $fusionOwned[[int]$fusionProcess.ProcessId]=$fusionProcess }
    }
}
# A venv launcher can exit before its child. Verify listener identity independently
# instead of depending on a dead parent PID or terminating an unknown port owner.
$fusionPorts=@(8010,8011,5178,5179,4041)
foreach ($fusionListener in @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in $fusionPorts })) {
    $fusionProcess=Get-CimInstance Win32_Process -Filter "ProcessId=$($fusionListener.OwningProcess)" -ErrorAction SilentlyContinue
    if (Test-FusionOwnedProcess $fusionProcess $fusionRoot) { $fusionOwned[[int]$fusionProcess.ProcessId]=$fusionProcess }
}
foreach ($fusionProcess in $fusionOwned.Values) {
    $fusionTree=@([int]$fusionProcess.ProcessId)
    for ($fusionLevel=0;$fusionLevel -lt 6;$fusionLevel++) {
        $fusionChildren=@(Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -in $fusionTree -and $_.ProcessId -notin $fusionTree -and $_.CreationDate -ge $fusionProcess.CreationDate } | Select-Object -ExpandProperty ProcessId)
        if (-not $fusionChildren.Count) { break }
        $fusionTree+=$fusionChildren
    }
    [array]::Reverse($fusionTree)
    foreach ($fusionProcessId in $fusionTree) { Stop-Process -Id $fusionProcessId -ErrorAction SilentlyContinue }
}
$fusionDeadline=(Get-Date).AddSeconds(8)
do {
    $fusionRemaining=@(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in $fusionPorts })
    if (-not $fusionRemaining.Count) { break }
    Start-Sleep -Milliseconds 300
} while ((Get-Date) -lt $fusionDeadline)
if ($fusionRemaining.Count) {
    foreach ($fusionListener in $fusionRemaining) { Write-Warning "端口 $($fusionListener.LocalPort) 仍由 PID $($fusionListener.OwningProcess) 占用。未强行结束未知进程。" }
    throw '关闭未完成，请核对上述进程归属后重试；没有将残留端口报告为成功。'
}
if (-not $fusionCloudFailed) {
    Save-FusionProcessRecords $fusionPidPath @()
    Save-FusionProcessRecords $fusionVoicePidPath @()
}
if ($StopInfrastructure) {
    Push-Location -LiteralPath $fusionRoot
    try { docker compose --profile vector stop; if ($LASTEXITCODE -ne 0) { throw '本工程容器停止失败。' } } finally { Pop-Location }
}
if ($fusionCloudFailed) { throw '本地服务已关闭，云端任务状态仍需核对。' }
Write-Output '本工程进程已停止；8010/8011/5178/5179/4041 端口已释放，数据库数据卷保留。'