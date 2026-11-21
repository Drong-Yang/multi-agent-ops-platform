const test = require('node:test');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const executable = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');

function run(command) {
  return spawnSync(executable, ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', command],
    { cwd: root, env: { ...process.env, FUSION_TEST_ROOT: root }, encoding: 'utf8', windowsHide: true, timeout: 30000 });
}

test('Windows PowerShell 5.1 can parse all launch and stop scripts including Chinese strings', () => {
  const result = run("$ErrorActionPreference='Stop'; foreach ($file in @('start.ps1','stop.ps1','start-voice.ps1','stop-voice.ps1')) { $tokens=$null; $errors=$null; [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $env:FUSION_TEST_ROOT $file),[ref]$tokens,[ref]$errors) > $null; if ($errors.Count) { throw ($file+': '+$errors[0].Message) } }; Write-Output 'PARSE_OK'");
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /PARSE_OK/);
});

test('Windows PowerShell 5.1 can run the offline voice preflight without launching cloud tasks', () => {
  const result = run("& (Join-Path $env:FUSION_TEST_ROOT 'start-voice.ps1') -CheckOnly");
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /"cloud_tasks_started": 0/);
});

test('the CMD entry can run offline preflight without changing the caller execution policy', () => {
  const before = run('Get-ExecutionPolicy -Scope CurrentUser').stdout;
  const result = spawnSync(process.env.ComSpec || 'cmd.exe', ['/d', '/c', 'start-voice.cmd -CheckOnly'],
    { cwd: root, encoding: 'utf8', windowsHide: true, timeout: 30000 });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /"cloud_tasks_started": 0/);
  assert.equal(run('Get-ExecutionPolicy -Scope CurrentUser').stdout, before);
});
