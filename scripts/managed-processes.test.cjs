const test = require('node:test');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const ps = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
function run(script) {
  const result = spawnSync(ps, ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command',
    "$ErrorActionPreference='Stop'; . (Join-Path $env:FUSION_TEST_ROOT 'scripts/managed-processes.ps1'); " + script],
    { cwd: root, env: { ...process.env, FUSION_TEST_ROOT: root }, windowsHide: true, encoding: 'utf8', timeout: 20000 });
  assert.equal(result.status, 0, result.stderr);
  return result.stdout.trim();
}

test('PS5 JSON arrays and historical value/Count wrappers become individual valid PID records', () => {
  const out = run("$data=ConvertFrom-Json '[{\"id\":11,\"name\":\"backend\"},{\"value\":[{\"id\":22,\"name\":\"voice\"}],\"Count\":1},{\"id\":\"bad\",\"name\":\"frontend\"}]'; $items=@(ConvertTo-FusionProcessRecords -InputObject $data); if($items.Count -ne 2 -or $items[0].id -ne 11 -or $items[1].id -ne 22){throw 'PID records were not flattened'}; Write-Output 'FLATTEN_OK'");
  assert.match(out, /FLATTEN_OK/);
});

test('an orphaned own venv interpreter is recognized but unrelated Python and prefix-similar paths are not', () => {
  const out = run("$r=$env:FUSION_TEST_ROOT; $own=[pscustomobject]@{CommandLine=('\"'+$r+'\\backend\\.venv\\Scripts\\python.exe\" run_server.py')}; $other=[pscustomobject]@{CommandLine='python.exe unrelated.py'}; $prefix=[pscustomobject]@{CommandLine=('\"'+$r+'-other\\backend\\.venv\\Scripts\\python.exe\" run_server.py')}; if(-not(Test-FusionOwnedProcess $own $r)){throw 'orphan not recognized'}; if((Test-FusionOwnedProcess $other $r) -or (Test-FusionOwnedProcess $prefix $r)){throw 'foreign process accepted'}; Write-Output 'OWNERSHIP_OK'");
  assert.match(out, /OWNERSHIP_OK/);
});

test('new process records serialize flat and round-trip in Windows PowerShell 5.1', () => {
  const out = run("$file=Join-Path $env:FUSION_TEST_ROOT ('.runtime/pid-contract-'+[guid]::NewGuid().ToString('N')+'.json'); try { $items=@([pscustomobject]@{name='backend';id=12},[pscustomobject]@{value=@([pscustomobject]@{name='voice';id=34});Count=1}); Save-FusionProcessRecords $file $items; $read=@(Read-FusionProcessRecords $file); if($read.Count -ne 2 -or $read[1].id -ne 34){throw 'roundtrip failed'}; Write-Output 'ROUNDTRIP_OK' } finally { if(Test-Path -LiteralPath $file){Remove-Item -LiteralPath $file} }");
  assert.match(out, /ROUNDTRIP_OK/);
});

test('JSON health bytes are decoded as UTF-8 rather than PS5 default Latin-1', () => {
  const out = run("$expected=-join[char[]]@(30005,21830,36816,33829,65,103,101,110,116); $bytes=[Text.Encoding]::UTF8.GetBytes(('{\"service\":\"'+$expected+'\",\"mock\":false}')); $health=Read-FusionJsonUtf8 $bytes; if($health.service -ne $expected -or $health.mock -ne $false){throw 'health JSON decoded incorrectly'}; Write-Output 'UTF8_OK'");
  assert.match(out, /UTF8_OK/);
});
