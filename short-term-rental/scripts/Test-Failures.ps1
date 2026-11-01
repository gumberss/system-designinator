param([string]$BaseUrl = 'http://localhost:58080')

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
$bookingId = $null

function Invoke-Compose([string[]]$ComposeArguments) {
    & docker compose @ComposeArguments
    if ($LASTEXITCODE -ne 0) { throw "Docker Compose falhou: $ComposeArguments" }
}

function Wait-Ready {
    for ($i = 0; $i -lt 60; $i++) {
        try {
            $ready = Invoke-RestMethod "$BaseUrl/health/ready"
            if ($ready.status -eq 'ready') { return }
        } catch {
            Write-Host "Aguardando primary: $($_.Exception.Message)"
        }
        Start-Sleep -Seconds 1
    }
    throw 'API nao ficou pronta em 60 tentativas.'
}

try {
    Wait-Ready
    $property = (Invoke-RestMethod "$BaseUrl/properties?limit=1")[0]
    $start = [DateTime]::UtcNow.Date.AddDays((Get-Random -Minimum 60001 -Maximum 70000))
    $key = [guid]::NewGuid().ToString()
    $body = @{
        propertyId = $property.id
        guestId = [guid]::NewGuid().ToString()
        checkIn = $start.ToString('yyyy-MM-dd')
        checkOut = $start.AddDays(2).ToString('yyyy-MM-dd')
    } | ConvertTo-Json

    Invoke-Compose -ComposeArguments @('stop', 'rabbitmq')
    $response = Invoke-WebRequest -UseBasicParsing -Method Post -Uri "$BaseUrl/bookings" `
        -ContentType 'application/json' -Headers @{'Idempotency-Key' = $key} -Body $body
    if ($response.StatusCode -ne 201) { throw 'Reserva com broker parado deveria retornar 201.' }
    $bookingId = ($response.Content | ConvertFrom-Json).id
    $data = Invoke-RestMethod "$BaseUrl/diagnostics"
    if ($data.pendingEvents -lt 1) { throw 'Evento deveria permanecer na outbox durante falha do broker.' }
    Write-Host 'PASS: broker parado, reserva confirmada e evento pendente no PostgreSQL.'
    Invoke-Compose -ComposeArguments @('start', 'rabbitmq')

    Invoke-Compose -ComposeArguments @('stop', 'redis')
    $metadata = Invoke-RestMethod "$BaseUrl/properties/$($property.id)"
    if ($metadata.id -ne $property.id) { throw 'Fallback de catalogo retornou imovel incorreto.' }
    Write-Host 'PASS: Redis parado, metadados lidos do PostgreSQL.'
    Invoke-Compose -ComposeArguments @('start', 'redis')

    Invoke-Compose -ComposeArguments @('stop', 'postgres')
    $status = 0
    try {
        $unexpected = Invoke-WebRequest -UseBasicParsing "$BaseUrl/health/ready"
        $status = [int]$unexpected.StatusCode
    } catch {
        if ($null -eq $_.Exception.Response) { throw }
        $status = [int]$_.Exception.Response.StatusCode
    }
    if ($status -ne 503) { throw "Primary parado deveria retornar 503; recebido $status." }
    Write-Host 'PASS: PostgreSQL parado, API nao declara readiness/sucesso falso.'
    Invoke-Compose -ComposeArguments @('start', 'postgres')
    Wait-Ready

    $replay = Invoke-RestMethod -Method Post -Uri "$BaseUrl/bookings" `
        -ContentType 'application/json' -Headers @{'Idempotency-Key' = $key} -Body $body
    if ($replay.id -ne $bookingId) { throw 'Retry apos recuperar deveria retornar a mesma reserva.' }

    $converged = $false
    for ($i = 0; $i -lt 120; $i++) {
        $data = Invoke-RestMethod "$BaseUrl/diagnostics"
        if ($data.pendingEvents -eq 0 -and $data.processedEvents -eq $data.totalEvents -and `
            $data.confirmedBookings -eq $data.projectedActiveBookings -and $data.overlappingPairs -eq 0) {
            $converged = $true
            break
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $converged) { throw 'Eventos/projecao nao convergiram apos recuperar servicos.' }
    Write-Host 'PASS: recuperacao, replay da mesma reserva e convergencia sem overlaps.'
} finally {
    Invoke-Compose -ComposeArguments @('start', 'postgres', 'redis', 'rabbitmq')
    Wait-Ready
    if ($null -ne $bookingId) {
        Invoke-RestMethod -Method Post "$BaseUrl/bookings/$bookingId/cancel" | Out-Null
    }
}
