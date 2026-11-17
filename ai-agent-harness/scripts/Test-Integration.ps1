$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$desktopClient = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe'
$docker = if (Test-Path -LiteralPath $desktopClient) { $desktopClient } else { 'docker' }
$project = 'harness-tests-' + [guid]::NewGuid().ToString('N').Substring(0, 10)
$keys = @(
    'HARNESS_POSTGRES_PASSWORD', 'HARNESS_REDIS_PASSWORD', 'HARNESS_POSTGRES_DB',
    'HARNESS_POSTGRES_PORT', 'HARNESS_REDIS_PORT',
    'HARNESS_TEST_DATABASE_URL', 'HARNESS_TEST_REDIS_URL',
    'HARNESS_OTLP_PORT', 'HARNESS_TEST_OTLP_ENDPOINT'
)
$previous = @{}
foreach ($key in $keys) { $previous[$key] = [Environment]::GetEnvironmentVariable($key) }

function Get-FreePort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    try { return $listener.LocalEndpoint.Port } finally { $listener.Stop() }
}

$cleanup = $false
$result = 1
Push-Location $root
try {
    if (-not (Test-Path -LiteralPath $python)) { throw 'Install the project dev environment first.' }
    & $docker version --format '{{.Server.Version}}'
    if ($LASTEXITCODE -ne 0) { throw 'Docker engine is unavailable. No integration tests were run.' }
    $env:HARNESS_POSTGRES_PASSWORD = [guid]::NewGuid().ToString('N')
    $env:HARNESS_REDIS_PASSWORD = [guid]::NewGuid().ToString('N')
    $env:HARNESS_POSTGRES_DB = 'harness_test'
    $env:HARNESS_POSTGRES_PORT = [string](Get-FreePort)
    $env:HARNESS_REDIS_PORT = [string](Get-FreePort)
    $env:HARNESS_OTLP_PORT = [string](Get-FreePort)
    $env:HARNESS_TEST_OTLP_ENDPOINT = "http://127.0.0.1:$($env:HARNESS_OTLP_PORT)"
    $env:HARNESS_TEST_DATABASE_URL = "postgresql://harness:$($env:HARNESS_POSTGRES_PASSWORD)@127.0.0.1:$($env:HARNESS_POSTGRES_PORT)/harness_test"
    $env:HARNESS_TEST_REDIS_URL = "redis://:$($env:HARNESS_REDIS_PASSWORD)@127.0.0.1:$($env:HARNESS_REDIS_PORT)/15"
    $cleanup = $true
    & $docker compose --profile observability --project-name $project up -d --wait --wait-timeout 90
    if ($LASTEXITCODE -ne 0) { throw 'Isolated PostgreSQL/Redis services did not become healthy.' }
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            $probe = Invoke-WebRequest -UseBasicParsing -Method Post `
                -Uri "$($env:HARNESS_TEST_OTLP_ENDPOINT)/v1/traces" `
                -ContentType 'application/json' -Body '{"resourceSpans":[]}' -TimeoutSec 2
            if ($probe.StatusCode -eq 200) { $ready = $true; break }
        } catch [System.Net.WebException] {
            Start-Sleep -Seconds 1
        }
    }
    if (-not $ready) { throw 'Isolated OpenTelemetry Collector did not become ready.' }
    & $python -m pytest -q -m integration
    $result = $LASTEXITCODE
}
finally {
    if ($cleanup) {
        & $docker compose --profile observability --project-name $project down --volumes
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Cleanup failed for this task's Compose project: $project"
            $result = 1
        }
    }
    foreach ($key in $keys) {
        $path = 'Env:\' + $key
        if ($null -eq $previous[$key]) {
            if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path }
        } else {
            Set-Item -LiteralPath $path -Value $previous[$key]
        }
    }
    Pop-Location
}
exit $result
