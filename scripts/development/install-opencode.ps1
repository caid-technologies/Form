[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

function Fail-Setup {
    param([Parameter(Mandatory = $true)][string]$Message)
    throw "forma install: $Message"
}

$repoUrl = if ([string]::IsNullOrWhiteSpace($env:FORMA_OSS_REPO_URL)) {
    "https://github.com/caid-technologies/Forma-OSS.git"
} else {
    $env:FORMA_OSS_REPO_URL
}
$localAppData = if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    Join-Path $HOME "AppData\Local"
} else {
    $env:LOCALAPPDATA
}
$installDir = if ([string]::IsNullOrWhiteSpace($env:FORMA_OSS_INSTALL_DIR)) {
    Join-Path $localAppData "Forma\forma-oss"
} else {
    $env:FORMA_OSS_INSTALL_DIR
}
$workspaceDir = if ([string]::IsNullOrWhiteSpace($env:FORMA_WORKSPACE_DIR)) {
    Join-Path $HOME "forma-workspace"
} else {
    $env:FORMA_WORKSPACE_DIR
}

$git = Get-Command git -ErrorAction SilentlyContinue
if ($null -eq $git) {
    Fail-Setup "git was not found on PATH. Install Git and rerun this command."
}

$python = Get-Command py -ErrorAction SilentlyContinue
$pythonArgs = @("-3")
if ($null -eq $python) {
    $python = Get-Command python -ErrorAction SilentlyContinue
    $pythonArgs = @()
}
if ($null -eq $python) {
    Fail-Setup "Python 3.11 or newer was not found on PATH. Install Python and rerun this command."
}

if ((Test-Path -LiteralPath $installDir) -and -not (Test-Path -LiteralPath (Join-Path $installDir ".git"))) {
    Fail-Setup "$installDir exists but is not a Forma Git checkout. Set FORMA_OSS_INSTALL_DIR to another path."
}

if (-not (Test-Path -LiteralPath (Join-Path $installDir ".git"))) {
    $parent = Split-Path -Parent $installDir
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $cloneArgs = @("clone", "--depth", "1")
    if (-not [string]::IsNullOrWhiteSpace($env:FORMA_OSS_REF)) {
        $cloneArgs += @("--branch", $env:FORMA_OSS_REF)
    }
    $cloneArgs += @($repoUrl, $installDir)
    & $git.Source @cloneArgs
    if ($LASTEXITCODE -ne 0) {
        Fail-Setup "Could not clone Forma OSS."
    }
} else {
    Write-Host "Using existing Forma checkout at $installDir"
}

$pythonExecutable = (& $python.Source @pythonArgs -c "import sys; print(sys.executable)" | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($pythonExecutable)) {
    Fail-Setup "Could not resolve the Python executable used for the local runtime."
}
$env:PYTHON_BIN = $pythonExecutable

$setupScript = Join-Path $installDir "scripts\development\setup-opencode.py"
& $python.Source @pythonArgs $setupScript --root $installDir --workspace $workspaceDir --install-cli
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Starting the local Forma backend and UI. Keep this terminal open."
Write-Host ""
& (Join-Path $installDir "scripts\development\dev.ps1")
