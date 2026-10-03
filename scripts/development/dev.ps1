[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

function Get-ConfiguredValue {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Default
    )

    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) {
        return $Default
    }
    return $value
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $false)][string[]]$ArgumentList = @()
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE`: $FilePath $($ArgumentList -join ' ')"
    }
}

function Test-PortOpen {
    param([Parameter(Mandatory = $true)][int]$Port)

    try {
        $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
        return $null -ne $connection
    } catch {
        return $false
    }
}

function Test-ProcessRunning {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    return $null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

function Test-RepoProcess {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $false
    }

    $command = "{0} {1}" -f $process.ExecutablePath, $process.CommandLine
    return $command.IndexOf($RootDir, [StringComparison]::OrdinalIgnoreCase) -ge 0
}

function Stop-RecordedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$PidFile,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not (Test-Path -LiteralPath $PidFile)) {
        return
    }

    $rawPid = (Get-Content -LiteralPath $PidFile -Raw).Trim()
    $processId = 0
    if ([int]::TryParse($rawPid, [ref]$processId) -and (Test-ProcessRunning $processId)) {
        if (Test-RepoProcess $processId) {
            Write-Host "[forma-dev] Stopping prior $Label process $processId..."
            & taskkill.exe /PID $processId /T /F *> $null
        } else {
            Write-Warning "Refusing to stop $Label process $processId because it is not associated with this checkout."
        }
    }

    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
}

function Wait-ForUrl {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $false)][int]$Attempts = 60,
        [Parameter(Mandatory = $false)][Nullable[int]]$ProcessId = $null
    )

    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
                Write-Host "[forma-dev] $Label is ready at $Url"
                return
            }
        } catch {
            # The service may still be starting.
        }

        if ($null -ne $ProcessId -and -not (Test-ProcessRunning $ProcessId.Value)) {
            throw "$Label process exited before becoming ready."
        }
        Start-Sleep -Seconds 1
    }

    throw "$Label did not become ready at $Url"
}

function Start-ManagedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $false)][string[]]$ArgumentList = @(),
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$PidFile,
        [Parameter(Mandatory = $true)][string]$OutputFile,
        [Parameter(Mandatory = $true)][string]$ErrorFile
    )

    $parent = Split-Path -Parent $PidFile
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $outputParent = Split-Path -Parent $OutputFile
    New-Item -ItemType Directory -Path $outputParent -Force | Out-Null

    $process = Start-Process `
        -FilePath $FilePath `
        -ArgumentList $ArgumentList `
        -WorkingDirectory $WorkingDirectory `
        -RedirectStandardOutput $OutputFile `
        -RedirectStandardError $ErrorFile `
        -PassThru
    Set-Content -LiteralPath $PidFile -Value $process.Id -Encoding ascii
    return $process.Id
}

$RootDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location -LiteralPath $RootDir

$BackendHost = Get-ConfiguredValue "BACKEND_HOST" "127.0.0.1"
$BackendPort = [int](Get-ConfiguredValue "BACKEND_PORT" "8000")
$FrontendHost = Get-ConfiguredValue "FRONTEND_HOST" "127.0.0.1"
$RequestedFrontendPort = [int](Get-ConfiguredValue "FRONTEND_PORT" "3000")
$PythonBin = Get-ConfiguredValue "PYTHON_BIN" "python"
$VenvDir = Get-ConfiguredValue "VENV_DIR" (Join-Path $RootDir ".venv")
$BackendLogFile = Get-ConfiguredValue "BACKEND_LOG_FILE" (Join-Path $RootDir ".logs\backend-dev.log")
$LocalSecretsFile = Get-ConfiguredValue "FORMA_LOCAL_SECRETS_FILE" (Join-Path $RootDir ".forma\local-secrets.env")
$RuntimeDir = Get-ConfiguredValue "DEV_RUNTIME_DIR" (Join-Path $RootDir ".tmp\development")

if (-not [IO.Path]::IsPathRooted($LocalSecretsFile)) {
    $LocalSecretsFile = Join-Path $RootDir $LocalSecretsFile
}
if (-not [IO.Path]::IsPathRooted($BackendLogFile)) {
    $BackendLogFile = Join-Path $RootDir $BackendLogFile
}
if (-not [IO.Path]::IsPathRooted($VenvDir)) {
    $VenvDir = Join-Path $RootDir $VenvDir
}
if (-not [IO.Path]::IsPathRooted($RuntimeDir)) {
    $RuntimeDir = Join-Path $RootDir $RuntimeDir
}

$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPip = Join-Path $VenvDir "Scripts\pip.exe"
$BackendPidFile = Join-Path $RuntimeDir "backend.pid"
$FrontendPidFile = Join-Path $RuntimeDir "frontend.pid"
$BackendErrorLogFile = "$BackendLogFile.err"
$FrontendLogFile = Join-Path (Split-Path -Parent $BackendLogFile) "frontend-dev.log"
$FrontendErrorLogFile = "$FrontendLogFile.err"

$frontendPort = $RequestedFrontendPort
while (Test-PortOpen $frontendPort) {
    $frontendPort++
    if ($frontendPort -gt ($RequestedFrontendPort + 20)) {
        throw "No free frontend port found from $RequestedFrontendPort to $($RequestedFrontendPort + 20)."
    }
}

$env:FORMA_AUTH_MODE = Get-ConfiguredValue "FORMA_AUTH_MODE" "local"
$env:FORMA_DEPLOYMENT_MODE = Get-ConfiguredValue "FORMA_DEPLOYMENT_MODE" "local"
$env:FORMA_DEVELOPMENT_MODE = Get-ConfiguredValue "FORMA_DEVELOPMENT_MODE" "true"
$env:FORMA_DEV_MODE = Get-ConfiguredValue "FORMA_DEV_MODE" $env:FORMA_DEVELOPMENT_MODE
$env:BACKEND_LOG_FILE = $BackendLogFile
if ([string]::IsNullOrWhiteSpace($env:FORMA_WEB_URL)) {
    $env:FORMA_WEB_URL = "http://$FrontendHost`:$frontendPort"
}
# Do not select an LLM here. The connected host agent authors the IR, while
# Forma compiles it deterministically. Existing provider/model environment
# values are inherited unchanged for explicit server-side generation.

if ([string]::IsNullOrWhiteSpace($env:FORMA_USER_SECRETS_KEY) -and (Test-Path -LiteralPath $LocalSecretsFile)) {
    foreach ($line in Get-Content -LiteralPath $LocalSecretsFile) {
        if ($line -match '^FORMA_USER_SECRETS_KEY=(.*)$') {
            $env:FORMA_USER_SECRETS_KEY = $Matches[1].Trim()
            break
        }
    }
}

if ([string]::IsNullOrWhiteSpace($env:FORMA_USER_SECRETS_KEY)) {
    $secretsDirectory = Split-Path -Parent $LocalSecretsFile
    New-Item -ItemType Directory -Path $secretsDirectory -Force | Out-Null
    $env:FORMA_USER_SECRETS_KEY = (& $PythonBin -c "import secrets; print(secrets.token_urlsafe(48))" | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($env:FORMA_USER_SECRETS_KEY)) {
        throw "Could not generate FORMA_USER_SECRETS_KEY with $PythonBin."
    }
    Set-Content -LiteralPath $LocalSecretsFile -Value "FORMA_USER_SECRETS_KEY=$($env:FORMA_USER_SECRETS_KEY)" -Encoding ascii
    Write-Host "[forma-dev] Generated a local encryption key at $LocalSecretsFile"
}

$backendProcessId = $null
$frontendProcessId = $null
$ownsBackend = $false
$ownsFrontend = $false

try {
    Stop-RecordedProcess $FrontendPidFile "frontend"
    Stop-RecordedProcess $BackendPidFile "backend"

    if (-not (Test-Path -LiteralPath $VenvPython)) {
        Write-Host "[forma-dev] Creating Python virtualenv at $VenvDir"
        Invoke-Checked $PythonBin @("-m", "venv", $VenvDir)
    }

    & $VenvPython -m uvicorn --version *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[forma-dev] Installing backend dependencies"
        Invoke-Checked $VenvPip @("install", "-r", (Join-Path $RootDir "apps\api\requirements.txt"))
    }

    & $VenvPython -c "import forma_cli" *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[forma-dev] Installing the forma-oss CLI"
        Invoke-Checked $VenvPip @("install", "-e", $RootDir)
    }

    $npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if ($null -eq $npmCommand) {
        $npmCommand = Get-Command npm -ErrorAction SilentlyContinue
    }
    if ($null -eq $npmCommand) {
        throw "npm was not found on PATH. Install Node.js before starting Forma."
    }

    $frontendDirectory = Join-Path $RootDir "apps\web"
    if (-not (Test-Path -LiteralPath (Join-Path $frontendDirectory "node_modules"))) {
        Write-Host "[forma-dev] Installing frontend dependencies"
        Push-Location -LiteralPath $frontendDirectory
        try {
            Invoke-Checked $npmCommand.Source @("install")
        } finally {
            Pop-Location
        }
    }

    if (Test-PortOpen $BackendPort) {
        Wait-ForUrl "http://$BackendHost`:$BackendPort/api" "Backend"
        Write-Host "[forma-dev] Backend already appears to be running at http://$BackendHost`:$BackendPort"
    } else {
        Write-Host "[forma-dev] Starting backend at http://$BackendHost`:$BackendPort"
        Write-Host "[forma-dev] Backend log file: $BackendLogFile"
        $backendProcessId = Start-ManagedProcess `
            -FilePath $VenvPython `
            -ArgumentList @("-m", "uvicorn", "apps.api.main:app", "--host", $BackendHost, "--port", [string]$BackendPort) `
            -WorkingDirectory $RootDir `
            -PidFile $BackendPidFile `
            -OutputFile $BackendLogFile `
            -ErrorFile $BackendErrorLogFile
        $ownsBackend = $true
        Wait-ForUrl "http://$BackendHost`:$BackendPort/api" "Backend" 60 $backendProcessId
    }

    Write-Host "[forma-dev] Starting frontend at http://$FrontendHost`:$frontendPort"
    $frontendProcessId = Start-ManagedProcess `
        -FilePath $npmCommand.Source `
        -ArgumentList @("--prefix", "`"$frontendDirectory`"", "run", "dev", "--", "--hostname", $FrontendHost, "--port", [string]$frontendPort) `
        -WorkingDirectory $frontendDirectory `
        -PidFile $FrontendPidFile `
        -OutputFile $FrontendLogFile `
        -ErrorFile $FrontendErrorLogFile
    $ownsFrontend = $true
    Wait-ForUrl "http://$FrontendHost`:$frontendPort/" "Frontend" 60 $frontendProcessId

    Write-Host ""
    Write-Host "Forma is running:"
    Write-Host "  Backend:  http://$BackendHost`:$BackendPort"
    Write-Host "  Frontend: http://$FrontendHost`:$frontendPort"
    Write-Host ""
    Write-Host "Press Ctrl+C to stop both services."

    while ($true) {
        if ($ownsBackend -and -not (Test-ProcessRunning $backendProcessId)) {
            throw "Backend process exited. See $BackendLogFile and $BackendErrorLogFile."
        }
        if ($ownsFrontend -and -not (Test-ProcessRunning $frontendProcessId)) {
            throw "Frontend process exited. See $FrontendLogFile and $FrontendErrorLogFile."
        }
        Start-Sleep -Seconds 1
    }
} finally {
    if ($ownsFrontend -and $null -ne $frontendProcessId -and (Test-ProcessRunning $frontendProcessId) -and (Test-RepoProcess $frontendProcessId)) {
        & taskkill.exe /PID $frontendProcessId /T /F *> $null
    }
    if ($ownsBackend -and $null -ne $backendProcessId -and (Test-ProcessRunning $backendProcessId) -and (Test-RepoProcess $backendProcessId)) {
        & taskkill.exe /PID $backendProcessId /T /F *> $null
    }
    if ($ownsFrontend -and (Test-Path -LiteralPath $FrontendPidFile)) {
        Remove-Item -LiteralPath $FrontendPidFile -Force -ErrorAction SilentlyContinue
    }
    if ($ownsBackend -and (Test-Path -LiteralPath $BackendPidFile)) {
        Remove-Item -LiteralPath $BackendPidFile -Force -ErrorAction SilentlyContinue
    }
}
