# 设置云端值守的自动启动（只需运行一次）：把 GitHub 令牌存进 Cloudflare Worker 的加密密钥 GH_TOKEN，并部署带定时任务的 Worker。
# 令牌由你本人在 GitHub 创建、在这个窗口粘贴，不会写进任何文件。
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$W = "wrangler@4"
Write-Host ""
Write-Host "== 第 1 步：在 GitHub 创建令牌（浏览器会自动打开创建页面）==" -ForegroundColor Cyan
Write-Host "  Token name   : aq5-cloud-kick"
Write-Host "  Expiration   : 选最长（例如 1 年），到期后重新运行本脚本"
Write-Host "  Repository access : Only select repositories -> 选 Suqi854/-A"
Write-Host "  Permissions  : Repository permissions -> Actions -> Read and write（其他都不用开）"
Write-Host "  点 Generate token，复制以 github_pat_ 开头的令牌"
Start-Process "https://github.com/settings/personal-access-tokens/new"
Write-Host ""
$sec = Read-Host "== 第 2 步：把令牌粘贴到这里后回车（输入时不显示）" -AsSecureString
$tok = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
if (-not ($tok -match '^(github_pat_|ghp_)')) { Write-Host "这看起来不是 GitHub 令牌（应以 github_pat_ 开头），已取消。" -ForegroundColor Red; exit 1 }
Write-Host "== 第 3 步：写入 Cloudflare Worker 密钥 GH_TOKEN ==" -ForegroundColor Cyan
$o = ($tok | cmd /c "npx --yes $W secret put GH_TOKEN 2>&1" | Out-String)
$tok = $null
if ($o -notmatch "Success") { Write-Host $o; Write-Host "写入失败：请确认已登录 Cloudflare（可先运行 deploy_fixed_url.cmd）" -ForegroundColor Red; exit 1 }
Write-Host "== 第 4 步：部署 Worker（含交易日每 5 分钟的云端值守检查）==" -ForegroundColor Cyan
$d = (cmd /c "npx --yes $W deploy 2>&1" | Out-String)
if ($d -notmatch "workers\.dev") { Write-Host $d; Write-Host "部署失败" -ForegroundColor Red; exit 1 }
Write-Host ""
Write-Host "完成！交易日 08:15-15:50 每 5 分钟会检查一次：没有云端值守在跑就自动启动。" -ForegroundColor Green
Write-Host "查看最近一次检查结果：https://aq5-relay.phehwm62784.workers.dev/__aq5/cloud"