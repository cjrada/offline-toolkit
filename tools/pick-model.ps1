# Model selection for start.bat (Windows PowerShell 5.1+). Same rule as tools/pick-model.sh:
#  1. GPU: largest models\*.gguf with size + 1024 MB <= VRAM (nvidia-smi, else the display adapter's
#     memory from the registry / Win32_VideoController).
#  2. RAM: largest models\*.gguf with size + 4096 MB <= RAM.   3. Else the smallest, 4k context.
# models\spare\ is never auto-picked. models\x.gguf pairs with models\x.mmproj.gguf (vision projector).
# Prints KEY=VALUE lines (MODEL, MMPROJ, CTX, REASON) for start.bat. Respects $env:MODEL / CTX / GPU.
# $env:GPU_FALLBACK=1 (start.bat sets it with GPU=disable after llamafile exited in GPU mode) notes the
# fallback in REASON and models.json. $env:LLM_RUN (start counter) is copied into models.json.
# Also prints LOAD_TIMEOUT: $env:HEALTH_TIMEOUT, else 60 s + 1 s per 50 MB of model, and GPU_MB: the GPU
# memory seen for this pick (0 = no GPU, or GPU=disable); start.bat only falls back to the CPU after a
# GPU attempt, i.e. when GPU_MB > 0. GPU_CC lists NVIDIA compute capabilities (start.bat turns off Vulkan
# coopmat2 on 7.5 / Turing, where it crashes on long prompts).
#   -Json FILE         also write the home page's www\models.json
#   -RequestFile FILE  read a model-switch request (from the 127.0.0.1 control server): line 1 must equal
#                      $env:SWITCH_TOKEN, line 2 must name an existing model under models\, optional line 3
#                      must be exactly cpu=on or cpu=off (home-page "Use CPU only" toggle); else exit 1
#   -CheckOnly         with -RequestFile: only validate, print REQ_MODEL=... and REQ_CPU=on|off|keep
param([string]$Json = '', [string]$RequestFile = '', [switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$kit = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $kit
$head = if ($env:HEADROOM_MB) { [int64]$env:HEADROOM_MB } else { 4096 }
$ghead = if ($env:GPU_HEADROOM_MB) { [int64]$env:GPU_HEADROOM_MB } else { 1024 }

$ramMB = 0
if ($env:RAM_MB) { $ramMB = [int64]$env:RAM_MB }
else {
  try { $ramMB = [int64]((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1MB) }
  catch { try { $ramMB = [int64]((Get-WmiObject Win32_ComputerSystem).TotalPhysicalMemory / 1MB) } catch { $ramMB = 0 } }
}

$ctx = $env:CTX
$model = $env:MODEL
if ($RequestFile) {
  $lines = @()
  try { $lines = @(Get-Content -LiteralPath $RequestFile -TotalCount 3 -ErrorAction Stop) } catch {}
  $tok = if ($lines.Count -ge 1) { ([string]$lines[0]).Trim() } else { '' }
  $req = if ($lines.Count -ge 2) { ([string]$lines[1]).Trim() } else { '' }
  $cpu = if ($lines.Count -ge 3) { [string]$lines[2] } else { '' }
  if (-not $env:SWITCH_TOKEN -or $tok -cne $env:SWITCH_TOKEN) { [Console]::Error.WriteLine('bad or missing switch token'); exit 1 }
  if ($req -notmatch '^models/(spare/)?[A-Za-z0-9._-]+\.gguf$' -or $req -match '\.\.' -or $req -match 'mmproj') {
    [Console]::Error.WriteLine('invalid model-switch request'); exit 1 }
  if (-not ($cpu -ceq '' -or $cpu -ceq 'cpu=on' -or $cpu -ceq 'cpu=off')) { [Console]::Error.WriteLine('invalid cpu flag'); exit 1 }
  $model = $req -replace '/', '\'
  if (-not (Test-Path -LiteralPath $model -PathType Leaf)) { [Console]::Error.WriteLine('requested model not found'); exit 1 }
  if ($CheckOnly) { "REQ_MODEL=$model"; "REQ_CPU=" + $(if ($cpu) { $cpu.Substring(4) } else { 'keep' }); exit 0 }
  if ($cpu -ceq 'cpu=on') { $env:GPU = 'disable'; $env:GPU_FALLBACK = '' }
  elseif ($cpu -ceq 'cpu=off') { $env:GPU = $(if ($env:USER_GPU -ne 'disable') { $env:USER_GPU } else { '' }); $env:GPU_FALLBACK = '' }
}

$vramMB = 0
$ccs = @()
if ($env:GPU -eq 'disable') { $vramMB = 0 }
elseif ($env:VRAM_MB) { $vramMB = [int64]$env:VRAM_MB }
else {
  try {
    $smi = Get-Command nvidia-smi -ErrorAction Stop
    foreach ($l in (& $smi.Source --query-gpu=memory.total --format=csv,noheader,nounits)) {
      $v = [int64]($l -replace '[^0-9]', ''); if ($v -gt $vramMB) { $vramMB = $v } }
  } catch {}
  # NVIDIA compute capability (e.g. 7.5 = Turing / RTX 20xx), queried separately: older nvidia-smi
  # builds lack compute_cap, and that must not cost the VRAM reading above.
  try {
    foreach ($l in (& $smi.Source --query-gpu=compute_cap --format=csv,noheader 2>$null)) {
      if ("$l".Trim() -match '^[0-9]+\.[0-9]+$') { $ccs += @("$l".Trim()) } }
  } catch {}
  if ($vramMB -eq 0) {
    # qwMemorySize is a 64-bit value (AdapterRAM is capped at 4 GB); covers AMD and NVIDIA.
    try {
      Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0*' -ErrorAction SilentlyContinue |
        ForEach-Object { $q = $_.'HardwareInformation.qwMemorySize'; if ($q) { $v = [int64]([uint64]$q / 1MB); if ($v -gt $vramMB) { $vramMB = $v } } }
    } catch {}
  }
  if ($vramMB -eq 0) {
    try { Get-CimInstance Win32_VideoController | ForEach-Object { $v = [int64]([uint64]$_.AdapterRAM / 1MB); if ($v -gt $vramMB) { $vramMB = $v } } } catch {}
  }
}

function SizeMB($f) { [int64][math]::Ceiling($f.Length / 1MB) }
function Fit($mb) {
  if ($vramMB -gt 0 -and ($mb + $ghead) -le $vramMB) { 'gpu' }
  elseif ($ramMB -gt 0 -and ($mb + $head) -le $ramMB) { 'ram' }
  elseif ($ramMB -gt 0 -and ($mb + 2048) -le $ramMB) { 'tight' }
  else { 'too-big' }
}
function Models($dir) {
  @(Get-ChildItem -LiteralPath $dir -Filter '*.gguf' -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notmatch 'mmproj' } | Sort-Object Length)
}

if ($model) {
  if (-not (Test-Path -LiteralPath $model -PathType Leaf)) { [Console]::Error.WriteLine("MODEL=$model does not exist"); exit 1 }
  $fit = Fit (SizeMB (Get-Item -LiteralPath $model)); $reason = "chosen by you ($fit)"
} else {
  $all = Models 'models'
  if ($all.Count -eq 0) { [Console]::Error.WriteLine('No .gguf model in models\'); exit 1 }
  $gpu = @($all | Where-Object { (Fit (SizeMB $_)) -eq 'gpu' })
  $ram = @($all | Where-Object { $ramMB -gt 0 -and ((SizeMB $_) + $head) -le $ramMB })
  if ($gpu.Count -gt 0) { $m = $gpu[-1]; $fit = 'gpu'; $reason = "largest model that fits the GPU ($vramMB MB VRAM)" }
  elseif ($ram.Count -gt 0) { $m = $ram[-1]; $fit = 'ram'; $reason = "largest model that fits $ramMB MB RAM (+$head MB headroom)" }
  else {
    $m = $all[0]; $fit = 'tight'
    $reason = if ($ramMB -eq 0) { 'could not read RAM size; smallest model' } else { "nothing fits $ramMB MB RAM comfortably; smallest model, 4k context (slow)" }
  }
  $model = 'models\' + $m.Name
}
$fallback = ($env:GPU_FALLBACK -eq '1')
$cpuOnly = ($env:GPU -eq 'disable')
if ($fallback) { $reason = "GPU mode failed, running on the CPU; $reason" }
elseif ($cpuOnly) { $reason = "CPU only; $reason" }
if (-not $ctx) { $ctx = if ($fit -eq 'gpu' -or $fit -eq 'ram') { '8192' } else { '4096' } }
$mm = $model -replace '\.gguf$', '.mmproj.gguf'
$mmproj = if (Test-Path -LiteralPath $mm -PathType Leaf) { $mm } else { '' }

if ($Json) {
  $list = @()
  foreach ($d in @('models', 'models\spare')) {
    foreach ($f in (Models $d)) {
      if ($f.Name -notmatch '^[A-Za-z0-9._-]+$') { continue }
      $p = ($d -replace '\\', '/') + '/' + $f.Name
      $list += [ordered]@{ path = $p; name = $f.BaseName; mb = (SizeMB $f); fit = (Fit (SizeMB $f));
                           spare = ($d -ne 'models'); vision = (Test-Path -LiteralPath (($d + '\' + $f.BaseName) + '.mmproj.gguf')) }
    }
  }
  $o = [ordered]@{ current = ($model -replace '\\', '/'); ctx = [int]$ctx; reason = $reason; ram_mb = $ramMB; vram_mb = $vramMB;
                   gpu_fallback = $fallback; cpu_only = $cpuOnly; run = $(if ($env:LLM_RUN -match '^[0-9]+$') { [int]$env:LLM_RUN } else { 0 });
                   switch_port = 8079; switch_token = ($env:SWITCH_TOKEN -replace '[^0-9a-f]', ''); models = $list }
  try { [IO.File]::WriteAllText((Join-Path $kit $Json), (ConvertTo-Json $o -Depth 4 -Compress)) } catch {}
}
"MODEL=$model"
"MMPROJ=$mmproj"
"CTX=$ctx"
"REASON=$reason"
$loadTimeout = if ($env:HEALTH_TIMEOUT -match '^[0-9]+$') { [int64]$env:HEALTH_TIMEOUT } else { 60 + [int64][math]::Floor((SizeMB (Get-Item -LiteralPath $model)) / 50) }
"LOAD_TIMEOUT=$loadTimeout"
"GPU_MB=$vramMB"
# GPU_CC: NVIDIA compute capabilities seen (space-separated, empty = none / not NVIDIA / GPU=disable).
"GPU_CC=" + (($ccs | Select-Object -Unique) -join ' ')
