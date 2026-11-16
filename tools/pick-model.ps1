# Windows twin of tools/pick-model.sh (same rules). Called by start.bat; prints one line:
#   MODEL|MMPROJ|CTX|REASON     (MMPROJ is '-' when there is none)
# Env overrides: MODEL (path or "auto"), CTX, VISION=1, KIT_RAM_MB (testing).
$ErrorActionPreference = 'Stop'
$kit = Split-Path -Parent $PSScriptRoot
function SizeMB($p) { [int64]((Get-Item -LiteralPath $p).Length / 1MB) }
function CtxMB($c) { if ($c -ge 8192) { 1536 } elseif ($c -ge 4096) { 768 } else { 384 } }
function Rel($p) { if ($p) { $p.Substring($kit.Length).TrimStart('\') } else { '-' } }  # '-' = none (batch for /f collapses empty fields)
function MmprojFor($model) {
  $dir = Split-Path -Parent $model; $stem = [IO.Path]::GetFileNameWithoutExtension($model)
  $m = Get-ChildItem -LiteralPath $dir -Filter "$stem-mmproj*.gguf" -File -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($m) { return $m.FullName }
  $all = @(Get-ChildItem -LiteralPath $dir -Filter *.gguf -File)
  $proj = @($all | Where-Object Name -like '*mmproj*'); $mods = @($all | Where-Object Name -notlike '*mmproj*')
  if ($mods.Count -eq 1 -and $proj.Count -ge 1) { return $proj[0].FullName }
  return ''
}

if ($env:KIT_RAM_MB) { $ram = [int64]$env:KIT_RAM_MB }
else { $ram = [int64]((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1MB) }
$os = if ($ram -le 6144) { 1536 } else { 2048 }

if ($env:MODEL -and $env:MODEL -ne 'auto') {
  $m = if (Test-Path -LiteralPath $env:MODEL) { (Resolve-Path -LiteralPath $env:MODEL).Path } else { Join-Path $kit $env:MODEL }
  if (-not (Test-Path -LiteralPath $m)) { Write-Error "MODEL=$($env:MODEL) not found"; exit 1 }
  $ctx = if ($env:CTX) { $env:CTX } else { 8192 }
  $proj = MmprojFor $m; if (-not $proj) { $proj = '-' }
  Write-Output ("{0}|{1}|{2}|set by MODEL=" -f $m, $proj, $ctx); exit 0
}

$models = @(Get-ChildItem -LiteralPath (Join-Path $kit 'models') -Filter *.gguf -File -ErrorAction SilentlyContinue |
            Where-Object Name -notlike '*mmproj*')
if ($models.Count -eq 0) { Write-Error 'No .gguf model in models\'; exit 1 }
$cands = foreach ($f in $models) {
  $p = MmprojFor $f.FullName
  $mb = (SizeMB $f.FullName) + $(if ($p) { SizeMB $p } else { 0 })
  [pscustomobject]@{ Path = $f.FullName; Proj = $p; MB = $mb; Vision = [bool]$p }
}
$ctxList = if ($env:CTX) { @([int]$env:CTX) } else { @(8192, 4096, 2048) }
$modes = if ($env:VISION -eq '1') { @($true, $false) } else { @($false, $true) }
foreach ($vision in $modes) {
  foreach ($ctx in $ctxList) {
    $budget = $ram - $os - (CtxMB $ctx)
    $best = $cands | Where-Object { $_.Vision -eq $vision -and $_.MB -le $budget } | Sort-Object MB -Descending | Select-Object -First 1
    if ($best) {
      $why = "best fit for $ram MB RAM" + $(if ($vision) { ' (vision)' } else { '' })
      Write-Output ("{0}|{1}|{2}|{3}" -f (Rel $best.Path), (Rel $best.Proj), $ctx, $why); exit 0
    }
  }
}
$best = $cands | Sort-Object MB | Select-Object -First 1
$ctx = if ($env:CTX) { $env:CTX } else { 2048 }
Write-Output ("{0}|{1}|{2}|WARNING: only {3} MB RAM - even the smallest model may be slow or fail; close other apps" -f (Rel $best.Path), (Rel $best.Proj), $ctx, $ram)
