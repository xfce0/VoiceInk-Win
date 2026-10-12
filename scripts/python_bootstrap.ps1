param(
    [switch]$Check,
    [switch]$EnsureVenv,
    [string]$VenvPath = ".venv"
)

$ErrorActionPreference = "Stop"
$RequiredPython = "3.13.11"
$Root = (Resolve-Path (Join-Path $PSScriptRoot ".." )).Path

function Get-TargetArchitecture {
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw "VoiceInk Windows frontend packaging requires 64-bit Windows; the current PySide6 build has no 32-bit wheel."
    }
    return "x64"
}

function Get-PythonArchitecture([string]$PythonPath) {
    try {
        $value = & $PythonPath -c "import platform,sys; print(f'{sys.version_info.major}.{sys.version_info.minor}|{platform.machine()}')" 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        return ($value | Select-Object -Last 1).Trim()
    } catch {
        return $null
    }
}

function Test-Python([string]$PythonPath, [string]$TargetArchitecture) {
    if (-not (Test-Path $PythonPath)) { return $false }
    $info = Get-PythonArchitecture $PythonPath
    if ($null -eq $info) { return $false }
    $parts = $info.Split("|")
    if ($parts[0] -notin @("3.12", "3.13", "3.14")) { return $false }
    $machine = $parts[1].ToUpperInvariant()
    if ($TargetArchitecture -eq "x64") {
        return $machine -in @("AMD64", "X86_64")
    }
    return $machine -in @("X86", "I386", "I686")
}

function Find-Python([string]$TargetArchitecture) {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -ne $launcher) {
        $launcherArch = "64"
        $candidate = & $launcher.Source "-3.13-$launcherArch" -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and (Test-Python $candidate.Trim() $TargetArchitecture)) {
            return $candidate.Trim()
        }
    }

    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -ne $python -and (Test-Python $python.Source $TargetArchitecture)) {
        return $python.Source
    }
    return $null
}

function Install-Python([string]$TargetArchitecture) {
    $suffix = if ($TargetArchitecture -eq "x64") { "amd64" } else { "win32" }
    $target = Join-Path $Root (".tools\python-{0}-{1}" -f $RequiredPython, $TargetArchitecture)
    $python = Join-Path $target "python.exe"
    if (Test-Python $python $TargetArchitecture) {
        return $python
    }

    $downloadRoot = Join-Path $Root ".tools\downloads"
    New-Item -ItemType Directory -Force -Path $downloadRoot | Out-Null
    $installer = Join-Path $downloadRoot ("python-{0}-{1}.exe" -f $RequiredPython, $suffix)
    $temporaryInstaller = "$installer.download"
    $url = "https://www.python.org/ftp/python/$RequiredPython/python-$RequiredPython-$suffix.exe"
    Write-Host "Downloading Python $RequiredPython ($TargetArchitecture)"
    Invoke-WebRequest -Uri $url -OutFile $temporaryInstaller
    $signature = Get-AuthenticodeSignature -FilePath $temporaryInstaller
    if ($signature.Status -ne "Valid" -or $signature.SignerCertificate.Subject -notmatch "Python Software Foundation") {
        Remove-Item -Force $temporaryInstaller -ErrorAction SilentlyContinue
        throw "Downloaded Python installer failed Authenticode validation."
    }
    Move-Item -Force $temporaryInstaller $installer
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    & $installer /quiet InstallAllUsers=0 PrependPath=0 Include_test=0 TargetDir=$target
    if ($LASTEXITCODE -ne 0 -or -not (Test-Python $python $TargetArchitecture)) {
        throw "Python $RequiredPython ($TargetArchitecture) installation failed."
    }
    return $python
}

function Resolve-Python {
    $target = Get-TargetArchitecture
    $python = Find-Python $target
    if ($null -eq $python) {
        $python = Install-Python $target
    }
    Write-Host "Using Python: $python"
    return $python
}

$python = Resolve-Python
if ($Check) {
    & $python (Join-Path $Root "scripts\windows_platform.py") --check
    exit $LASTEXITCODE
}

if ($EnsureVenv) {
    $venv = Join-Path $Root $VenvPath
    $venvPython = Join-Path $venv "Scripts\python.exe"
    $target = Get-TargetArchitecture
    if (-not (Test-Python $venvPython $target)) {
        if (Test-Path $venv) {
            Remove-Item -Recurse -Force $venv
        }
        & $python -m venv $venv
        if ($LASTEXITCODE -ne 0 -or -not (Test-Python $venvPython $target)) {
            throw "Could not create a $target virtual environment."
        }
    }
    Write-Host "Using virtual environment: $venvPython"
}
