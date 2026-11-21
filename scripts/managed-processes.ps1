# Keep this helper ASCII-only for both Windows PowerShell 5.1 and PowerShell 7.
function Read-FusionJsonUtf8 {
    param([byte[]]$Bytes)
    ConvertFrom-Json ([Text.Encoding]::UTF8.GetString($Bytes).TrimStart([char]0xFEFF))
}

function Get-FusionHealth {
    param([string]$Uri='http://127.0.0.1:8010/health', [int]$TimeoutSec=2)
    # PS5 Invoke-RestMethod defaults to Latin-1 for JSON lacking a charset.
    $response=Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec $TimeoutSec
    $buffer=New-Object IO.MemoryStream
    try {
        if ($response.RawContentStream.CanSeek) { $response.RawContentStream.Position=0 }
        $response.RawContentStream.CopyTo($buffer)
        Read-FusionJsonUtf8 $buffer.ToArray()
    } finally { $buffer.Dispose() }
}

function ConvertTo-FusionProcessRecords {
    param($InputObject)
    if ($null -eq $InputObject) { return }
    if ($InputObject -is [System.Array]) {
        foreach ($item in $InputObject) { ConvertTo-FusionProcessRecords -InputObject $item }
        return
    }
    if ($InputObject.PSObject.Properties['value'] -and -not $InputObject.PSObject.Properties['id']) {
        ConvertTo-FusionProcessRecords -InputObject $InputObject.value
        return
    }
    $processNumber = 0
    if ([int]::TryParse([string]$InputObject.id, [ref]$processNumber) -and $processNumber -gt 0 -and $InputObject.name) {
        [pscustomobject]@{name=[string]$InputObject.name; id=$processNumber}
    }
}

function Read-FusionProcessRecords {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path) {
        $parsed = ConvertFrom-Json ([IO.File]::ReadAllText($Path))
        ConvertTo-FusionProcessRecords -InputObject $parsed
    }
}

function Save-FusionProcessRecords {
    param([string]$Path, $Records)
    $flat = @(ConvertTo-FusionProcessRecords -InputObject $Records | Group-Object id | ForEach-Object { $_.Group[0] })
    $json = ConvertTo-Json -InputObject @($flat) -Depth 6
    [IO.File]::WriteAllText($Path, $json, (New-Object Text.UTF8Encoding($true)))
}

function Test-FusionOwnedProcess {
    param($Process, [string]$Root)
    if (-not $Process -or -not $Process.CommandLine) { return $false }
    $command = [string]$Process.CommandLine
    $comparison = [StringComparison]::OrdinalIgnoreCase
    $venv = Join-Path $Root 'backend\.venv\Scripts\python.exe'
    if ($command.IndexOf($venv,$comparison) -ge 0) {
        if ($command -match '(?:^|\s)run_server\.py(?:\s|$)' -or $command.IndexOf((Join-Path $Root 'scripts\run_voice_backend.py'),$comparison) -ge 0) { return $true }
        if ($command.Contains('http.server 5179') -and $command.IndexOf((Join-Path $Root 'frontend-voice\build'),$comparison) -ge 0) { return $true }
    }
    foreach ($relative in @('frontend-ops\node_modules\vite\bin\vite.js','frontend-voice\node_modules\@craco\craco\dist\bin\craco.js','scripts\callback-gateway.cjs')) {
        if ($command.IndexOf((Join-Path $Root $relative),$comparison) -ge 0) { return $true }
    }
    if ($command -match '\bstart\s+fusion_voice_test\b' -and $command.IndexOf((Join-Path $Root '.runtime\ngrok-voice.log'),$comparison) -ge 0) { return $true }
    return $false
}
