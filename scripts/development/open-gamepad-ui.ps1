[CmdletBinding()]
param(
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 3000,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$WebDir = Join-Path $RootDir "apps\web"
$ProjectId = "03940f0b-0223-4fa3-921e-9ef3026e670f"
$LogDir = Join-Path $RootDir ".logs"

function Test-PortOpen {
    param([Parameter(Mandatory = $true)][int]$Port)

    try {
        return $null -ne (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop)
    } catch {
        return $false
    }
}

function Test-FormaProcess {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $false
    }
    $command = "{0} {1}" -f $process.ExecutablePath, $process.CommandLine
    return $command.IndexOf($RootDir, [StringComparison]::OrdinalIgnoreCase) -ge 0
}

function Stop-FormaPortProcess {
    param([Parameter(Mandatory = $true)][int]$Port)

    $connections = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    foreach ($connection in $connections) {
        if (Test-FormaProcess $connection.OwningProcess) {
            Write-Host "[gamepad-ui] Restarting Forma process $($connection.OwningProcess) on port $Port..."
            & taskkill.exe /PID $connection.OwningProcess /T /F *> $null
        }
    }
}

function Wait-ForUrl {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$Label,
        [int]$Attempts = 60
    )

    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400) {
                return
            }
        } catch {
            # The service may still be compiling or starting.
        }
        Start-Sleep -Seconds 1
    }
    throw "$Label did not become ready at $Url"
}

Set-Location -LiteralPath $RootDir
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

$env:FORMA_AUTH_MODE = "local"
$env:FORMA_DEV_MODE = "true"
$env:NEXT_PUBLIC_API_URL = "http://127.0.0.1:$BackendPort"

if (-not (Test-PortOpen $BackendPort)) {
    $python = Join-Path $RootDir ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python)) {
        $pythonCommand = Get-Command python -ErrorAction Stop
        $python = $pythonCommand.Source
    }
    Start-Process -FilePath $python `
        -ArgumentList @("-m", "uvicorn", "apps.api.main:app", "--host", "127.0.0.1", "--port", [string]$BackendPort) `
        -WorkingDirectory $RootDir `
        -RedirectStandardOutput (Join-Path $LogDir "gamepad-backend.log") `
        -RedirectStandardError (Join-Path $LogDir "gamepad-backend.err") `
        -WindowStyle Hidden | Out-Null
}
Wait-ForUrl "http://127.0.0.1:$BackendPort/" "Backend"

$projectUrl = "http://127.0.0.1:$BackendPort/projects/$ProjectId"
try {
    $projectResponse = Invoke-WebRequest -Uri $projectUrl -UseBasicParsing -TimeoutSec 5
    if ($projectResponse.StatusCode -lt 200 -or $projectResponse.StatusCode -ge 400) {
        throw "HTTP $($projectResponse.StatusCode)"
    }
} catch {
    throw "The seeded gamepad project was not found at $projectUrl."
}

if (Test-PortOpen $FrontendPort) {
    try {
        $frontendResponse = Invoke-WebRequest -Uri "http://127.0.0.1:$FrontendPort/project/$ProjectId" -UseBasicParsing -TimeoutSec 5
        if ($frontendResponse.StatusCode -ge 500) {
            Stop-FormaPortProcess $FrontendPort
            Start-Sleep -Milliseconds 500
            if (Test-Path -LiteralPath (Join-Path $WebDir ".next")) {
                Remove-Item -LiteralPath (Join-Path $WebDir ".next") -Recurse -Force
            }
        }
    } catch {
        Stop-FormaPortProcess $FrontendPort
        Start-Sleep -Milliseconds 500
    }
}

if (-not (Test-PortOpen $FrontendPort)) {
    Start-Process -FilePath "npm.cmd" `
        -ArgumentList @("--prefix", $WebDir, "run", "dev", "--", "--hostname", "127.0.0.1", "--port", [string]$FrontendPort) `
        -WorkingDirectory $WebDir `
        -RedirectStandardOutput (Join-Path $LogDir "gamepad-frontend.log") `
        -RedirectStandardError (Join-Path $LogDir "gamepad-frontend.err") `
        -WindowStyle Hidden | Out-Null
}

$uiUrl = "http://127.0.0.1:$FrontendPort/project/$ProjectId"
Wait-ForUrl $uiUrl "Forma UI"
Write-Host "[gamepad-ui] OpenCAD viewport project: $uiUrl"
Write-Host "[gamepad-ui] Select the CAD tab beside Mechanical and Billing Materials."

if (-not $NoBrowser) {
    Start-Process $uiUrl | Out-Null
}
