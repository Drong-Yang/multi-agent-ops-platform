param([switch]$Live, [switch]$WithVector, [switch]$SkipVoice)
$ErrorActionPreference = 'Stop'
$fusionRoot = $PSScriptRoot
. (Join-Path $fusionRoot 'scripts\managed-processes.ps1')
$fusionBackend = Join-Path $fusionRoot 'backend'
$fusionPython = Join-Path $fusionBackend '.venv\Scripts\python.exe'
$fusionNode = (Get-Command node -ErrorAction Stop).Source
if (-not (Test-Path -LiteralPath $fusionPython)) {
    throw '先在 backend 运行 uv sync --frozen，安装 Python 3.13 依赖。'
}
& $fusionPython (Join-Path $fusionRoot 'scripts\configure_local.py')
Push-Location -LiteralPath $fusionRoot
try {
    if ($WithVector) { docker compose --profile vector up -d --wait }
    else { docker compose up -d --wait postgres }
    if ($LASTEXITCODE -ne 0) { throw '独立数据库或检索服务启动失败。' }
    $fusionVite = Join-Path $fusionRoot 'frontend-ops\node_modules\vite\bin\vite.js'
    if (-not (Test-Path -LiteralPath $fusionVite)) { throw '先在 frontend-ops 运行 npm ci。' }
    $fusionRuntime = Join-Path $fusionRoot '.runtime'
    $fusionMockBefore = $env:MOCK_MODE
    $fusionRetrievalBefore = $env:FUSION_ACTIVATE_RETRIEVAL
    $env:MOCK_MODE = if ($Live) { 'false' } else { 'true' }
    if (-not $Live) { $env:FUSION_ACTIVATE_RETRIEVAL = 'sample' }
    $fusionPids = @()
    try {
        $fusionHealthy = $false
        try {
            $fusionHealth = Get-FusionHealth
            $fusionHealthy = $fusionHealth.service -eq '电商运营Agent'
        } catch {}
        if ($fusionHealthy -and ($fusionHealth.mock -ne (-not $Live))) {
            throw '服务已用另一生成模式运行。请先运行 stop.cmd，再选择是否使用 -Live 启动。'
        }
        if (-not $fusionHealthy) {
            $fusionProcess = Start-Process -FilePath $fusionPython -ArgumentList @('run_server.py') -WorkingDirectory $fusionBackend -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $fusionRuntime 'backend-out.log') -RedirectStandardError (Join-Path $fusionRuntime 'backend-error.log')
            $fusionPids += @{name='backend';id=$fusionProcess.Id}
        }
        $fusionWebOpen = Get-NetTCPConnection -State Listen -LocalPort 5178 -ErrorAction SilentlyContinue
        if (-not $fusionWebOpen) {
            $fusionProcess = Start-Process -FilePath $fusionNode -ArgumentList @($fusionVite,'--host','127.0.0.1','--port','5178','--strictPort') -WorkingDirectory (Join-Path $fusionRoot 'frontend-ops') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $fusionRuntime 'frontend-out.log') -RedirectStandardError (Join-Path $fusionRuntime 'frontend-error.log')
            $fusionPids += @{name='frontend';id=$fusionProcess.Id}
        }
        $fusionVoiceStart = Join-Path $fusionRoot 'frontend-voice\node_modules\@craco\craco\dist\bin\craco.js'
        $fusionVoiceBuild = Join-Path $fusionRoot 'frontend-voice\build'
        if (-not $SkipVoice -and ((Test-Path -LiteralPath $fusionVoiceStart) -or (Test-Path -LiteralPath $fusionVoiceBuild))) {
            if (-not (Get-NetTCPConnection -State Listen -LocalPort 5179 -ErrorAction SilentlyContinue)) {
                if (Test-Path -LiteralPath $fusionVoiceBuild) {
                    $fusionProcess = Start-Process -FilePath $fusionPython -ArgumentList @('-m','http.server','5179','--bind','127.0.0.1','--directory',$fusionVoiceBuild) -WorkingDirectory $fusionRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $fusionRuntime 'voice-out.log') -RedirectStandardError (Join-Path $fusionRuntime 'voice-error.log')
                } else {
                    $env:PORT='5179';$env:BROWSER='none';$env:HOST='127.0.0.1'
                    $fusionProcess = Start-Process -FilePath $fusionNode -ArgumentList @($fusionVoiceStart,'start') -WorkingDirectory (Join-Path $fusionRoot 'frontend-voice') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $fusionRuntime 'voice-out.log') -RedirectStandardError (Join-Path $fusionRuntime 'voice-error.log')
                }
                $fusionPids += @{name='voice';id=$fusionProcess.Id}
            }
        }
    } finally {
        $env:MOCK_MODE=$fusionMockBefore
        $env:FUSION_ACTIVATE_RETRIEVAL=$fusionRetrievalBefore
        # Even a partial launch must leave enough ownership information to stop it.
        $fusionPidPath=Join-Path $fusionRuntime 'pids.json'
        $fusionPids += @(Read-FusionProcessRecords $fusionPidPath)
        Save-FusionProcessRecords $fusionPidPath $fusionPids
    }
    Write-Output '工作台：http://127.0.0.1:5178；API：http://127.0.0.1:8010/docs'
} finally { Pop-Location }
