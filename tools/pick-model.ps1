# Auto model selection for start.bat (Windows PowerShell 5.1+). Same rule as tools/pick-model.sh:
# the LARGEST models\*.gguf whose size + 3072 MB headroom fits in RAM; else the smallest with a 4k context.
# models\spare\ is never auto-picked. models\x.gguf pairs with models\x.mmproj.gguf (vision projector).
# Prints KEY=VALUE lines (MODEL, MMPROJ, CTX, REASON) for start.bat to read. Respects $env:MODEL / $env:CTX.
$ErrorActionPreference = 'Stop'
$kit = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $kit
$headroom = if ($env:HEADROOM_MB) { [int64]$env:HEADROOM_MB } else { 3072 }
$ramMB = 0
if ($env:RAM_MB) { $ramMB = [int64]$env:RAM_MB }
else {
  try { $ramMB = [int64]((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1MB) }
  catch { try { $ramMB = [int64]((Get-WmiObject Win32_ComputerSystem).TotalPhysicalMemory / 1MB) } catch { $ramMB = 0 } }
}
$ctx = $env:CTX
if ($env:MODEL) {
  if (-not (Test-Path -LiteralPath $env:MODEL -PathType Leaf)) { [Console]::Error.WriteLine("MODEL=$($env:MODEL) does not exist"); exit 1 }
  $model = $env:MODEL; $reason = 'MODEL set by you'
} else {
  $all = @(Get-ChildItem -LiteralPath 'models' -Filter '*.gguf' -File -ErrorAction SilentlyContinue |
           Where-Object { $_.Name -notmatch 'mmproj' } | Sort-Object Length)
  if ($all.Count -eq 0) { [Console]::Error.WriteLine('No .gguf model in models\'); exit 1 }
  $fit = @($all | Where-Object { $ramMB -gt 0 -and ([math]::Ceiling($_.Length / 1MB) + $headroom) -le $ramMB })
  if ($fit.Count -gt 0) {
    $m = $fit[-1]; $reason = "largest model that fits $ramMB MB RAM"
  } else {
    $m = $all[0]
    if ($ramMB -eq 0) { $reason = 'could not read RAM size; using the smallest model' }
    else { $reason = "nothing fits $ramMB MB RAM comfortably; smallest model, 4k context"; if (-not $ctx) { $ctx = '4096' } }
  }
  $model = 'models\' + $m.Name
}
if (-not $ctx) { $ctx = '8192' }
$mm = $model -replace '\.gguf$', '.mmproj.gguf'
$mmproj = if (Test-Path -LiteralPath $mm -PathType Leaf) { $mm } else { '' }
"MODEL=$model"
"MMPROJ=$mmproj"
"CTX=$ctx"
"REASON=$reason"
