# 部署固定地址中转（只需运行一次）。需要 Node.js；第一次会打开浏览器让你登录 Cloudflare。
# 全部输出同时写到 deploy_log.txt，方便排查。
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$W = "wrangler@4"
$LOG = Join-Path $PSScriptRoot "deploy_log.txt"
"== $(Get-Date -Format s) ==" | Out-File $LOG -Encoding utf8

function Run([string]$a, [string]$stdin = $null) {
  "`n>>> wrangler $a" | Out-File $LOG -Append -Encoding utf8
  if ($stdin) { $o = ($stdin | cmd /c "npx --yes $W $a 2>&1" | Out-String) }
  else { $o = (cmd /c "npx --yes $W $a 2>&1" | Out-String) }
  $o | Out-File $LOG -Append -Encoding utf8
  Write-Host $o
  return $o
}
function Login {
  Write-Host ""
  Write-Host ">>> 浏览器（Edge）会打开 Cloudflare 授权页：登录后点【Allow / 允许】，2 分钟内完成" -ForegroundColor Yellow
  cmd /c "npx --yes $W logout" | Out-Null
  & cmd /c "npx --yes $W login"
}
function Fail([string]$m) {
  "FAIL: $m" | Out-File $LOG -Append -Encoding utf8
  Write-Host "失败：$m" -ForegroundColor Red
  Write-Host "详细输出在 deploy_log.txt"
  exit 1
}

Write-Host "== 1/4 检查 Cloudflare 登录 =="
$who = Run "whoami"
if ($who -match "not authenticated|You are not logged in|missing some expected Oauth scopes") {
  Write-Host "需要重新授权：浏览器会打开 Cloudflare 页面，登录后点 Allow（允许）" -ForegroundColor Yellow
  Login; $who = Run "whoami"
}
# 多个账号时取第一个账号 ID
if ($who -match '([0-9a-f]{32})') { $env:CLOUDFLARE_ACCOUNT_ID = $Matches[1]; "account=$($Matches[1])" | Out-File $LOG -Append -Encoding utf8 }

if (Select-String -Path wrangler.toml -Pattern 'id = "REPLACE_ME"' -Quiet) {
  Write-Host "== 2/4 创建 KV 存储 =="
  $out = Run "kv namespace create aq5_relay"
  if ($out -match "code: 10000") {
    Write-Host "授权不完整，重新登录一次（浏览器里点 Allow）。如果是新注册的账号，请先去邮箱点验证链接。" -ForegroundColor Yellow
    Login
    $out = Run "kv namespace create aq5_relay"
  }
  if ($out -notmatch '([0-9a-f]{32})') {
    $ls = Run "kv namespace list"
    if ($ls -match '"id":\s*"([0-9a-f]{32})"\s*,\s*"title":\s*"[^"]*aq5_relay') { $kv = $Matches[1] }
  } else { $kv = $Matches[1] }
  if (-not $kv) { Fail "没有拿到 KV id" }
  $t = (Get-Content wrangler.toml -Raw) -replace 'id = "REPLACE_ME"', ('id = "' + $kv + '"')
  [IO.File]::WriteAllText((Join-Path $PSScriptRoot "wrangler.toml"), $t, (New-Object Text.UTF8Encoding($false)))
}
Write-Host "== 3/4 部署 Worker =="
$dep = Run "deploy"
if ($dep -notmatch '(https://aq5-relay\.[a-z0-9-]+\.workers\.dev)') { Fail "没有拿到 workers.dev 地址（第一次使用 workers.dev 可能需要先在 Cloudflare 面板 Workers 页面注册子域名）" }
$url = $Matches[1]
$secret = -join ((48..57) + (97..122) | Get-Random -Count 40 | ForEach-Object { [char]$_ })
Write-Host "== 4/4 设置密钥并告诉本机引擎 =="
Run "secret put SECRET" $secret | Out-Null
$body = @{ url = $url; secret = $secret } | ConvertTo-Json
$pushed = $false
for ($i = 0; $i -lt 6 -and -not $pushed; $i++) {
  Start-Sleep -Seconds 5
  try {
    $r = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:18950/api/remote/relay" -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($body))
    $pushed = [bool]$r.pushed
    "push try $i -> $($r.pushed)" | Out-File $LOG -Append -Encoding utf8
  } catch { "push try $i error $_" | Out-File $LOG -Append -Encoding utf8 }
}
if (-not $pushed) { Write-Host "中转已部署，但还没把电脑地址推上去（请确认已开启手机远程）；引擎每 5 分钟会自动重试。" -ForegroundColor Yellow }
"OK $url" | Out-File $LOG -Append -Encoding utf8
Write-Host ""
Write-Host "完成！固定地址：$url" -ForegroundColor Green
Write-Host "以后手机直接打开这个地址（或 GitHub 网页版解锁）就能连到电脑。"
