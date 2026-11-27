# bench/launch.ps1 -RunDir <run_dir>
# Starts the free LLM proxy, then a SEPARATE herdr server (session oke-bench) whose process env
# points every Claude at the proxy, opens the orchestrator pane and runs the env probe in it.
# It never starts claude (bench/driver.py does) and never touches the default herdr session.
# On a failed probe it stops only the oke-bench session and exits 1.
# Writes <run_dir>/bench.json: {"session", "pane", "workspace", "orch", "home"}.
param(
    [Parameter(Mandatory = $true)][string]$RunDir
)
$ErrorActionPreference = "Stop"
$Session = "oke-bench"
$Bench = $PSScriptRoot
$RunDir = (Resolve-Path $RunDir).Path
$Orch = Join-Path $RunDir "orch"
$Home2 = Join-Path $RunDir "home"
$Logs = Join-Path $RunDir "logs"
New-Item -ItemType Directory -Force $Logs | Out-Null

function Write-Log([string]$text) {
    $line = "{0} launch: {1}" -f (Get-Date -Format "s"), $text
    Write-Host $line
    Add-Content -Path (Join-Path $Logs "launch.log") -Value $line -Encoding utf8
}

function Invoke-Herdr {
    # herdr against the bench session only, with a timeout: nothing may hang
    param([string[]]$HerdrArgs, [int]$TimeoutSec = 30)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "herdr"
    $psi.Arguments = (@("--session", $Session) + $HerdrArgs | ForEach-Object {
            if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } }) -join " "
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $p = [System.Diagnostics.Process]::Start($psi)
    $outTask = $p.StandardOutput.ReadToEndAsync()
    $errTask = $p.StandardError.ReadToEndAsync()
    if (-not $p.WaitForExit($TimeoutSec * 1000)) {
        try { $p.Kill() } catch {}
        throw "herdr $($HerdrArgs -join ' ') timed out after $TimeoutSec s"
    }
    return [pscustomobject]@{ Code = $p.ExitCode; Out = $outTask.Result; Err = $errTask.Result }
}

function Stop-BenchSession([string]$why) {
    Write-Log "ABORT: $why; stopping only the $Session session"
    try { Invoke-Herdr -HerdrArgs @("server", "stop") -TimeoutSec 30 | Out-Null } catch { Write-Log "server stop: $_" }
    exit 1
}

if (-not (Test-Path (Join-Path $Home2 "config.json"))) { throw "no $Home2\config.json: run bench/setup.sh first" }
$cfg = Get-Content (Join-Path $Home2 "config.json") -Raw | ConvertFrom-Json
$Model = [string]$cfg.models.pm
if ($Model -notmatch '^free-[a-z0-9-]+$') { throw "config.json models.pm is not a free-* model" }
foreach ($role in "pm", "worker", "reviewer") {
    if ($cfg.models.$role -ne $Model) { throw "config.json models.$role is not $Model" }
}
$allowed = @($cfg.allowed_models)
if ($allowed.Count -eq 0 -or @($allowed | Where-Object { $_ -notlike "free-*" }).Count -gt 0) {
    throw "config.json allowed_models must list only free-* models"
}

# every herdr call reaches the bench session only: an inherited HERDR_* (a socket path) beats --session
Get-ChildItem Env: | Where-Object { $_.Name -like "HERDR_*" } | ForEach-Object {
    Remove-Item "Env:$($_.Name)" -ErrorAction SilentlyContinue
}

# 1. the bench session must not run yet (an old run: bench/teardown.py first)
$st = Invoke-Herdr -HerdrArgs @("status", "server")
if ($st.Out -match "status:\s*running") { throw "herdr session $Session already runs: run bench/teardown.py <old run dir> first" }

# 2. the proxy, and its key (loaded into this process only, never printed)
$proxyDir = Join-Path $env:USERPROFILE "litellm-proxy"
. (Join-Path $proxyDir "ensure-proxy.ps1") | Out-Null
try { Invoke-RestMethod -Uri "http://127.0.0.1:4000/health/liveliness" -TimeoutSec 5 | Out-Null }
catch { throw "LiteLLM proxy is not alive on http://127.0.0.1:4000" }
$token = $env:LITELLM_MASTER_KEY
if ([string]::IsNullOrWhiteSpace($token)) { throw "LITELLM_MASTER_KEY is empty after load-env.ps1" }
Write-Log "proxy alive; key loaded (not shown)"

# 3. the server's env: nothing of the caller's Claude, herdr or OKe session, and no provider keys
$keep = @("CLAUDE_CODE_GIT_BASH_PATH")
Get-ChildItem Env: | ForEach-Object {
    $n = $_.Name
    if (($n -match '^(CLAUDE|AI_AGENT|HERDR_|ANTHROPIC_|OKE_|SHELL$)' -or
            $n -in @("GEMINI_API_KEY", "NVIDIA_NIM_API_KEY", "OPENROUTER_API_KEY", "LITELLM_MASTER_KEY")) -and
        ($n -notin $keep)) {
        Remove-Item "Env:$n" -ErrorAction SilentlyContinue
    }
}
$env:CLAUDE_CONFIG_DIR = Join-Path $env:USERPROFILE ".claude-free"
$env:ANTHROPIC_AUTH_TOKEN = $token
$token = $null
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:4000"
$env:ANTHROPIC_MODEL = $Model
$env:ANTHROPIC_DEFAULT_OPUS_MODEL = $Model
$env:ANTHROPIC_DEFAULT_SONNET_MODEL = $Model
$env:ANTHROPIC_DEFAULT_FABLE_MODEL = $Model
$env:ANTHROPIC_DEFAULT_HAIKU_MODEL = "free-fast"
$env:CLAUDE_CODE_SUBAGENT_MODEL = $Model
$env:CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "1"
$env:OKE_HERDR = '["herdr","--session","oke-bench"]'
$env:OKE_HOME = $Home2
if (-not (Test-Path $env:CLAUDE_CONFIG_DIR)) { throw "$($env:CLAUDE_CONFIG_DIR) does not exist" }

# 4. the bench server, in the background with that env
$server = Start-Process -FilePath "herdr" -ArgumentList @("--session", $Session, "server") -WindowStyle Hidden -PassThru
Set-Content -Path (Join-Path $RunDir "bench-server.pid") -Value $server.Id -Encoding ascii
Write-Log "started herdr --session $Session server (pid $($server.Id))"
$deadline = (Get-Date).AddSeconds(30)
do {
    Start-Sleep -Milliseconds 500
    $st = Invoke-Herdr -HerdrArgs @("status", "server")
} until ($st.Out -match "status:\s*running" -or (Get-Date) -gt $deadline)
if ($st.Out -notmatch "status:\s*running") { Stop-BenchSession "server did not come up in 30 s" }

# 5. the orchestrator's pane: a workspace in the bench session, at orch/
$ws = Invoke-Herdr -HerdrArgs @("workspace", "create", "--cwd", $Orch, "--label", "oke-bench-orch", "--no-focus")
if ($ws.Code -ne 0) { Stop-BenchSession "workspace create failed: $($ws.Err.Trim())" }
Add-Content -Path (Join-Path $Logs "launch.log") -Value $ws.Out -Encoding utf8
$wsj = $ws.Out | ConvertFrom-Json
$pane = $null; $wsid = $null
foreach ($path in @("result.root_pane.pane_id", "result.pane.pane_id", "result.pane_id", "root_pane.pane_id", "pane_id")) {
    $v = $wsj
    foreach ($part in $path.Split(".")) { if ($null -ne $v) { $v = $v.$part } }
    if ($v) { $pane = [string]$v; break }
}
foreach ($path in @("result.workspace.workspace_id", "result.workspace_id", "workspace.workspace_id", "workspace_id")) {
    $v = $wsj
    foreach ($part in $path.Split(".")) { if ($null -ne $v) { $v = $v.$part } }
    if ($v) { $wsid = [string]$v; break }
}
if (-not $pane) { Stop-BenchSession "no pane id in workspace create output" }
Write-Log "orchestrator pane $pane (workspace $wsid)"

# 6. the probe, typed into that pane: the env claude will inherit there
function Read-Probe {
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 1
        $r = Invoke-Herdr -HerdrArgs @("pane", "read", $pane, "--source", "recent-unwrapped", "--lines", "200")
        $m = [regex]::Matches($r.Out, "BENCHPROBE-RESULT (.*?) BENCHPROBE-END")
        if ($m.Count -gt 0) { return $m[$m.Count - 1].Groups[1].Value }
    }
    return $null
}
Start-Sleep -Seconds 3  # the shell's first prompt
$orchQ = $Orch.Replace("\", "/")
Invoke-Herdr -HerdrArgs @("pane", "run", $pane, "cd '$orchQ'") | Out-Null
Start-Sleep -Seconds 1
$probe = "python '" + (Join-Path $Bench "probe.py").Replace("\", "/") + "' --home '" + $Home2.Replace("\", "/") + "' --cwd '$orchQ'"
Invoke-Herdr -HerdrArgs @("pane", "run", $pane, $probe) | Out-Null
$result = Read-Probe
if (-not $result) { Stop-BenchSession "the probe printed nothing in 60 s" }
Write-Log "probe: $result"
$bad = @([regex]::Matches($result, "(\w+)=no") | ForEach-Object { $_.Groups[1].Value })
if ($bad.Count -gt 0) { Stop-BenchSession ("probe failed: " + ($bad -join ", ")) }

$info = [ordered]@{ session = $Session; pane = $pane; workspace = $wsid; orch = $orchQ;
    home = $Home2.Replace("\", "/"); server_pid = $server.Id; probe = $result }
$info | ConvertTo-Json | Set-Content -Path (Join-Path $RunDir "bench.json") -Encoding utf8
Write-Log "env probe passed; ready for the driver"
exit 0
