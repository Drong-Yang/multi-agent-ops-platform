param(
    [switch]$CheckOnly,
    [string]$NgrokExe = '',
    [string]$NgrokConfig = ''
)
$ErrorActionPreference = 'Stop'
$fusionRoot = [IO.Path]::GetFullPath($PSScriptRoot)
. (Join-Path $fusionRoot 'scripts\managed-processes.ps1')
$fusionPython = Join-Path $fusionRoot 'backend\.venv\Scripts\python.exe'
if (-not $NgrokExe) { $NgrokExe = Join-Path (Split-Path (Split-Path $fusionRoot -Parent) -Parent) '.runtime\ngrok\ngrok.exe' }
if (-not $NgrokConfig) { $NgrokConfig = Join-Path $fusionRoot '.runtime\ngrok-voice.yml' }
if (-not (Test-Path -LiteralPath $NgrokExe)) { throw '未找到 ngrok。请通过 -NgrokExe 指定已安装的程序。' }
if (-not (Test-Path -LiteralPath $NgrokConfig)) { throw '缺少本工程 .runtime/ngrok-voice.yml；请参考 docs/MANUAL_ACCEPTANCE.md 配置。' }
& $fusionPython (Join-Path $fusionRoot 'scripts\voice_preflight.py') --ngrok-config $NgrokConfig
if ($LASTEXITCODE -ne 0) { throw '语音配置离线预检失败；尚未启动隧道或通话。' }
& $NgrokExe config check --config $NgrokConfig *> $null
if ($LASTEXITCODE -ne 0) { throw 'ngrok 本地配置检查失败；请核对配置，勿将 token 发到日志中。' }
if ($CheckOnly) { Write-Output '离线预检通过。未创建隧道、通话或模型请求。'; return }

# Fail before launching anything rather than replace another project or a Mock app.
foreach ($fusionPort in @(8010,8011,5178,5179,4041)) {
    if (Get-NetTCPConnection -State Listen -LocalPort $fusionPort -ErrorAction SilentlyContinue) {
        throw "端口 $fusionPort 已被占用。若为本工程，请先运行 .\stop-voice.cmd；其他应用请自行关闭或调整端口。"
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $fusionRoot 'frontend-voice\build\index.html'))) {
    throw '语音前端尚未构建。请在 frontend-voice 运行 npm run build 后重试。'
}
$fusionRuntime = Join-Path $fusionRoot '.runtime'
$fusionVoicePids = @()
$fusionEnvKeys = @('SERVER_URL','FUSION_ACTIVATE_RETRIEVAL','MILVUS_TIMEOUT','EMBEDDING_TIMEOUT')
$fusionBefore = @{}
foreach ($fusionKey in $fusionEnvKeys) { $fusionBefore[$fusionKey] = [Environment]::GetEnvironmentVariable($fusionKey,'Process') }
Push-Location -LiteralPath $fusionRoot
try {
    $fusionGateway = Start-Process -FilePath (Get-Command node).Source -ArgumentList @(('"{0}"' -f (Join-Path $fusionRoot 'scripts\callback-gateway.cjs'))) -WorkingDirectory $fusionRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $fusionRuntime 'callback-gateway-out.log') -RedirectStandardError (Join-Path $fusionRuntime 'callback-gateway-error.log')
    $fusionVoicePids += @{name='callback-gateway';id=$fusionGateway.Id}
    ConvertTo-Json -InputObject @($fusionVoicePids) | Set-Content -LiteralPath (Join-Path $fusionRuntime 'voice-validation-pids.json') -Encoding UTF8
    $fusionTunnel = Start-Process -FilePath $NgrokExe -ArgumentList @('start','fusion_voice_test','--config',('"{0}"' -f $NgrokConfig),'--log',('"{0}"' -f (Join-Path $fusionRuntime 'ngrok-voice.log')),'--log-format','json') -WorkingDirectory $fusionRoot -WindowStyle Hidden -PassThru
    $fusionVoicePids += @{name='ngrok';id=$fusionTunnel.Id}
    ConvertTo-Json -InputObject @($fusionVoicePids) | Set-Content -LiteralPath (Join-Path $fusionRuntime 'voice-validation-pids.json') -Encoding UTF8
    $fusionUrl = ''
    $fusionDeadline = (Get-Date).AddSeconds(35)
    while ((Get-Date) -lt $fusionDeadline) {
        if ($fusionTunnel.HasExited -or $fusionGateway.HasExited) { throw '隧道或回调网关提前退出；请查看本工程 .runtime 内日志。' }
        try {
            $fusionTunnels = Invoke-RestMethod 'http://127.0.0.1:4041/api/tunnels' -TimeoutSec 2
            $fusionSelected = @($fusionTunnels.tunnels | Where-Object { $_.name -eq 'fusion_voice_test' -and $_.public_url -like 'https://*' })
            if ($fusionSelected.Count -eq 1) { $fusionUrl = $fusionSelected[0].public_url; break }
        } catch {}
        Start-Sleep -Milliseconds 400
    }
    if (-not $fusionUrl) { throw '临时 HTTPS 隧道未就绪；请检查 ngrok 账号、网络和配额。' }
    # Environment applies to the child only; private files and original projects stay unchanged.
    $env:SERVER_URL = $fusionUrl
    $env:FUSION_ACTIVATE_RETRIEVAL = 'milvus'
    $env:MILVUS_TIMEOUT = '10'
    $env:EMBEDDING_TIMEOUT = '10'
    & (Join-Path $fusionRoot 'start.ps1') -Live -WithVector
    $fusionDeadline = (Get-Date).AddSeconds(60)
    $fusionReady = $false
    while ((Get-Date) -lt $fusionDeadline) {
        try {
            $fusionHealth = Get-FusionHealth
            if ($fusionHealth.service -eq '电商运营Agent' -and $fusionHealth.mock -eq $false -and $fusionHealth.retrieval_mode -eq 'milvus') { $fusionReady = $true; break }
        } catch {}
        Start-Sleep -Milliseconds 500
    }
    if (-not $fusionReady) { throw '真实模型/Milvus 服务未就绪。请检查 .runtime/backend-error.log；不会自动重建向量库或切换模拟模式。' }
    Write-Output '语音手测环境就绪：http://127.0.0.1:5178。登录后点击“打开语音渠道”，再点击“通话”。'
    Write-Output '尚未自动拨打通话。测试结束先挂断，再运行 .\stop-voice.cmd；可加 -StopInfrastructure 停止容器。'
} catch {
    # stop.ps1 only touches recorded project processes and attempts cloud cleanup first.
    & (Join-Path $fusionRoot 'stop.ps1')
    throw
} finally {
    foreach ($fusionKey in $fusionEnvKeys) { [Environment]::SetEnvironmentVariable($fusionKey,$fusionBefore[$fusionKey],'Process') }
    Pop-Location
}
