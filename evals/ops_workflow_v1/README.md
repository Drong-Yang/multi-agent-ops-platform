# 多 Agent 协同回归

从电商运营 Agent 仓库根目录执行。场景清单由 [scenarios.py](scenarios.py) 生成，执行入口为 [run.py](run.py)。每次执行写入新目录，保留 `scenarios.json`、`environment.json`、`invocation.json`、逐次 `results.jsonl` 和 `summary.json`。未选中的案例标记为 `not_run`。同一个 `case_id` 的失败重试仍按一个独立场景计数。

## SQLite 和历史回归

```powershell
$py = '.\backend\.venv\Scripts\python.exe'
$evalDir = 'evals/ops_workflow_v1'
$run = "$evalDir/runs/$(Get-Date -Format yyyyMMdd-HHmmss)"
New-Item -ItemType Directory -Path $run | Out-Null
$env:PYTHONIOENCODING = 'utf-8'

& $py "$evalDir/replay_baseline.py" "$run/baseline" *> "$run/baseline.log"

& $py "$evalDir/run.py" --select representative --out "$run/representative" *> "$run/representative.log"
& $py "$evalDir/run.py" --engine sqlite --repeats 10 --out "$run/sqlite" *> "$run/sqlite.log"
```

可以用 `--select critical --repeats 10` 单跑关键竞争，用 `--select recovery` 跑全部并发和恢复场景，或者用 `--select development`、`--select holdout` 按场景簇复跑。进度日志以最终状态码区分通过和失败，逐例结果含 HTTP 状态、路由、任务和证据 ID、快照、调用计数、异常和耗时。

单独复跑提交后取消和重复草稿场景。

```powershell
& $py "$evalDir/run.py" --case-id RC-pointer-cancel_after_commit `
    --case-id RC-draft_review-same_basis --out "$run/repro"
```

## PostgreSQL

以下命令使用已经缓存的 `postgres:16` 镜像。容器名按运行目录生成，数据位于临时文件系统，端口只发布在回环地址。账号和数据库限定为 `ops_regression`、`commerce_fusion_test`，每个新增场景单独建 schema 并清理。

```powershell
$port = 55493
if (netstat -ano -p tcp | Select-String ":$port\s+.*LISTENING") {
    throw "回环测试端口已占用，请选择另一个空闲端口"
}
$name = 'project11-ops-' + (Split-Path -Leaf $run)
if (docker ps -a --format '{{.Names}}' | Where-Object { $_ -eq $name }) {
    throw "容器名已被使用，请选择新运行目录"
}

$password = [Guid]::NewGuid().ToString('N')
$env:POSTGRES_PASSWORD = $password
try {
    $container = docker run -d --rm --pull=never --name $name `
        --label purpose=project11-isolated-regression --tmpfs /var/lib/postgresql/data `
        -p "127.0.0.1:${port}:5432" -e POSTGRES_USER=ops_regression `
        -e POSTGRES_PASSWORD -e POSTGRES_DB=commerce_fusion_test postgres:16
    if ($LASTEXITCODE -ne 0) { throw '临时 PostgreSQL 启动失败' }
} finally {
    Remove-Item Env:POSTGRES_PASSWORD -ErrorAction SilentlyContinue
}
$env:OPS_TEST_POSTGRES_URI = "postgresql://ops_regression:${password}@127.0.0.1:${port}/commerce_fusion_test"
try {
    docker ps --filter "id=$container" --format '{{.Names}} {{.Image}} {{.Ports}}'
    for ($i = 0; $i -lt 20; $i++) {
        docker exec $container pg_isready -q -h 127.0.0.1 -U ops_regression -d commerce_fusion_test
        if ($LASTEXITCODE -eq 0) { break }
        Start-Sleep -Milliseconds 500
    }
    if ($LASTEXITCODE -ne 0) { throw '临时 PostgreSQL 未就绪' }

    & $py scripts/run_tests.py --isolated-postgres --output-dir "$run/postgres-baseline" *> "$run/postgres-baseline.log"
    & $py scripts/run_tests.py --isolated-postgres --repository-postgres --output-dir "$run/postgres-baseline" *> "$run/repository-postgres.log"
    & $py "$evalDir/run.py" --engine postgres --repeats 10 --out "$run/postgres" *> "$run/postgres.log"
} finally {
    Remove-Item Env:OPS_TEST_POSTGRES_URI -ErrorAction SilentlyContinue
    docker stop $container | Out-Null
}
```

## 比较口径

- 修复前用基准提交 `b0394f7` 的后端文件和同一份冻结场景执行 SQLite、PostgreSQL 两次矩阵。命令与上文相同，省略 `--repeats 10`，分别写入新的输出目录。
- 修复后两种数据库执行相同的 240 个 ID。独立场景通过需要首次和所有附加执行都通过。关键竞争组记录数据库行、提交结果及索引指针，重复次数单独统计。
- 场景基于固定的 104 单元演示语料。RTC 测试使用应用内 ASGI 回调，PostgreSQL 使用真实事务。Milvus 就绪和同步、创作生成以及回答模型调用使用离线替身或调用拦截。
- 历史 Wiki 演练的 80 条保留原分母与原结果。本轮对照结果存放在本地 `runs/20261124-local/REPORT.md`。
