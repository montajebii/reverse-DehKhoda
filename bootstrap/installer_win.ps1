# installer_win.ps1 — Windows bootstrap actions (Docker Desktop / winget / WSL2).
#
# Called by the bundled Python bootstrap with:
#   powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
#                   -File installer_win.ps1 -Action <action> [-JsonOut <path>]
#
# The caller spawns us with CREATE_NO_WINDOW — no console ever becomes visible
# to the end user. The ONLY prompt shown is the native UAC dialog triggered by
# Start-Process -Verb RunAs for privileged actions.
#
# Actions:
#   detect          report Docker Desktop / winget / WSL2 state as JSON
#   install-docker  winget install Docker.DockerDesktop (elevated, silent)
#   enable-wsl2     wsl --install --no-distribution (elevated; may need reboot)
#   start-docker    launch Docker Desktop (user session, no elevation)
#   restart-computer  elevated reboot (used after WSL2 enablement)
#
# CI test hook: -AssumeNoDocker forces the "Docker Desktop absent" branch so
# the windows-latest runner (which may or may not ship Docker) exercises the
# deterministic fallback path.

param(
  [Parameter(Mandatory = $true)][ValidateSet(
    "detect", "install-docker", "enable-wsl2", "start-docker", "restart-computer")][string]$Action,
  [string]$JsonOut = "",
  [switch]$AssumeNoDocker
)

$ErrorActionPreference = "Stop"

function Test-DockerDesktop {
  param([switch]$ForceAbsent)
  $desktopExe = Join-Path ${env:ProgramFiles} "Docker\Docker\Docker Desktop.exe"
  $cli = Get-Command docker.exe -ErrorAction SilentlyContinue
  $installed = -not $ForceAbsent -and (($null -ne $cli) -or (Test-Path $desktopExe))
  $running = $false
  if ($installed -and ($null -ne $cli)) {
    & $cli.Source info 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $running = $true }
  }
  [pscustomobject]@{
    installed   = [bool]$installed
    running     = [bool]$running
    desktop_exe = if (Test-Path $desktopExe) { $desktopExe } else { $null }
    cli         = if ($null -ne $cli) { $cli.Source } else { $null }
  }
}

function Test-Winget {
  $w = Get-Command winget.exe -ErrorAction SilentlyContinue
  return [bool]$w
}

function Test-WSL2 {
  try {
    $out = & wsl.exe --version 2>$null
    return ($LASTEXITCODE -eq 0)
  } catch { return $false }
}

function Get-WindowsBuild {
  return [int](Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion").CurrentBuildNumber
}

function Invoke-Elevated {
  param([string]$Exe, [string[]]$ArgList, [int]$TimeoutSec = 1800)
  $p = Start-Process -FilePath $Exe -ArgumentList $ArgList -Verb RunAs `
         -Wait -PassThru -WindowStyle Hidden
  return $p.ExitCode
}

function Emit-Result {
  param([hashtable]$Result)
  $Result["os_type"] = "windows"
  $Result["windows_build"] = Get-WindowsBuild
  $Result["arch"] = if (${env:PROCESSOR_ARCHITEW6432}) { ${env:PROCESSOR_ARCHITEW6432} } else { $env:PROCESSOR_ARCHITECTURE }
  $json = $Result | ConvertTo-Json -Depth 4 -Compress
  if ($JsonOut -ne "") {
    Set-Content -Path $JsonOut -Value $json -Encoding UTF8
  } else {
    Write-Output "##JSON##$json"
  }
}

switch ($Action) {

  "detect" {
    $docker = Test-DockerDesktop -ForceAbsent:$AssumeNoDocker
    Emit-Result @{
      action  = "detect"
      ok      = $true
      docker  = $docker
      winget  = Test-Winget
      wsl2    = Test-WSL2
    }
  }

  "install-docker" {
    $winget = (Get-Command winget.exe -ErrorAction SilentlyContinue).Source
    if ($null -eq $winget) {
      Emit-Result @{ action = "install-docker"; ok = $false;
                     error_code = "winget_unavailable" }
      exit 0
    }
    # UAC prompt appears here (the one allowed elevation dialog).
    $rc = Invoke-Elevated -Exe $winget -ArgList @(
      "install", "-e", "--id", "Docker.DockerDesktop",
      "--silent", "--accept-package-agreements", "--accept-source-agreements"
    )
    $docker = Test-DockerDesktop
    Emit-Result @{ action = "install-docker"; ok = ($rc -eq 0);
                   winget_exit = $rc; docker_installed = $docker.installed }
  }

  "enable-wsl2" {
    # wsl --install --no-distribution enables the WSL2 optional components
    # without installing a Linux distro; a reboot is usually required.
    $out = & wsl.exe --install --no-distribution 2>&1
    $code = $LASTEXITCODE
    $rebootRequired = ($code -eq 3010) -or (($out -join " ") -match "restart|reboot")
    Emit-Result @{ action = "enable-wsl2"; ok = ($code -eq 0 -or $code -eq 3010);
                   exit_code = $code; reboot_required = [bool]$rebootRequired }
  }

  "start-docker" {
    $docker = Test-DockerDesktop
    if (-not $docker.installed) {
      Emit-Result @{ action = "start-docker"; ok = $false;
                     error_code = "docker_not_installed" }
      exit 0
    }
    Start-Process -FilePath $docker.desktop_exe -WindowStyle Hidden
    Emit-Result @{ action = "start-docker"; ok = $true }
  }

  "restart-computer" {
    $rc = Invoke-Elevated -Exe "shutdown.exe" -ArgList @("/r", "/t", "15")
    Emit-Result @{ action = "restart-computer"; ok = ($rc -eq 0) }
  }
}
