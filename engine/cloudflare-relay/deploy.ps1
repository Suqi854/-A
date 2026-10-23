# 部署固定地址中转（只需运行一次）。需要 Node.js；第一次会打开浏览器让你登录 Cloudflare。
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$W = "wrangler@4"
Write-Host "== 1/4 检查 Cloudflare 登录 =="
npx --yes $W whoami
if ($LASTEXITCODE -ne 0) { npx --yes $W login }
if (Select-String -Path wrangler.toml -Pattern 'id = "REPLACE_ME"' -Quiet) {
  Write-Host "== 2/4 创建 KV 存储 =="
  $out = (npx --yes $W kv namespace create aq5_relay 2>&1 | Out-String)
  Write-Host $out
  if ($out -match '([0-9a-f]{32})') {
    $t = (Get-Content wrangler.toml -Raw) -replace 'id = "REPLACE_ME"', ('id = "' + $Matches[1] + '"')
    [IO.File]::WriteAllText((Join-Path $PSScriptRoot "wrangler.toml"), $t, (New-Object Text.UTF8Encoding($false)))
  } else { throw "没有拿到 KV id" }
}
Write-Host "== 3/4 部署 Worker =="
$dep = (npx --yes $W deploy 2>&1 | Out-String)
Write-Host $dep
if ($dep -notmatch '(https://aq5-relay\.[a-z0-9-]+\.workers\.dev)') { throw "没有拿到 workers.dev 地址（第一次使用 workers.dev 可能需要先在 Cloudflare 面板里注册子域名）" }
$url = $Matches[1]
$secret = -join ((48..57) + (97..122) | Get-Random -Count 40 | ForEach-Object { [char]$_ })
Write-Host "== 4/4 设置密钥并告诉本机引擎 =="
$secret | npx --yes $W secret put SECRET
$body = @{ url = $url; secret = $secret } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:18950/api/remote/relay" -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($body)) | Out-Null
Write-Host ""
Write-Host "完成！固定地址：$url" -ForegroundColor Green
Write-Host "以后手机直接打开这个地址（或 GitHub 网页版解锁）就能连到电脑。"
