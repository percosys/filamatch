<#
.SYNOPSIS
  Install or update Filamatch in the Windows Fusion add-ins folder.

.DESCRIPTION
  Copies the Filamatch add-in folder to
  %APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\Filamatch
  and moves an old "FusionFilamentBridge" add-in (the name before 0.2.0) to
  the Recycle Bin, so Fusion does not load both.

  Restart Fusion afterwards (or Stop and Run the add-in in
  Utilities > ADD-INS > Scripts and Add-Ins).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install_addin_windows.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install_addin_windows.ps1 -DryRun
#>
param(
  [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Source = Join-Path $RepoRoot 'Filamatch'
$AddIns = Join-Path $env:APPDATA 'Autodesk\Autodesk Fusion 360\API\AddIns'
$Target = Join-Path $AddIns 'Filamatch'
$Legacy = Join-Path $AddIns 'FusionFilamentBridge'

Write-Host "Source add-in: $Source"
Write-Host "Fusion AddIns: $Target"

if (-not (Test-Path (Join-Path $Source 'Filamatch.manifest'))) {
  throw "Add-in source not found: $Source"
}

# Fusion silently ignores an add-in whose manifest is not valid JSON.
$manifest = Get-Content (Join-Path $Source 'Filamatch.manifest') -Raw | ConvertFrom-Json
if ($manifest.type -ne 'addin') {
  throw "Manifest type must be 'addin', got '$($manifest.type)'"
}
Write-Host "Manifest OK: Filamatch $($manifest.version)"

if ($DryRun) {
  Write-Host 'Dry run: nothing was copied.'
  if (Test-Path $Legacy) { Write-Host "Dry run: the old add-in would move to the Recycle Bin: $Legacy" }
  exit 0
}

if (Test-Path $Legacy) {
  Add-Type -AssemblyName Microsoft.VisualBasic
  [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory(
    $Legacy, 'OnlyErrorDialogs', 'SendToRecycleBin')
  Write-Host "Moved the old FusionFilament Bridge add-in to the Recycle Bin."
}

if (Test-Path $Target) {
  Remove-Item -Recurse -Force $Target
}
New-Item -ItemType Directory -Force -Path $Target | Out-Null
robocopy $Source $Target /E /XD __pycache__ /XF *.pyc .DS_Store | Out-Null
if ($LASTEXITCODE -ge 8) {
  throw "Copy failed (robocopy exit code $LASTEXITCODE)."
}

Write-Host ''
Write-Host 'Installed successfully:'
Write-Host "  $Target"
Write-Host 'Restart Fusion. The Filamatch button is on the UTILITIES tab, ADD-INS panel.'
