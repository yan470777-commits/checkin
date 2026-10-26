# 一键把 d:\签到 推送到 GitHub（供云端定时签到使用）
# 用法：
#   .\云端部署.ps1 -RepoUrl https://github.com/你的用户名/checkin.git
param(
    [Parameter(Mandatory = $true)][string]$RepoUrl,
    [string]$Branch = 'main'
)

# git 往 stderr 写提示是正常现象，不让它中断脚本
$ErrorActionPreference = 'Continue'
Set-Location $PSScriptRoot

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw '未安装 git，请先安装：https://git-scm.com/download/win'
}

if (-not (Test-Path '.git')) {
    Write-Host '[1/6] 初始化本地仓库...'
    git init | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'git init 失败' }
    git branch -M $Branch
} else {
    Write-Host '[1/6] 已是 git 仓库，跳过初始化'
}

# 安全兜底：凭证文件绝不能进仓库
if (Test-Path '.env') {
    $tracked = git ls-files '.env'
    if ($tracked) {
        Write-Host '[安全] .env 已被跟踪，取消跟踪（文件仍保留在本地）'
        git rm --cached '.env' | Out-Null
    }
}

Write-Host '[2/6] 检查关键文件...'
foreach ($f in @('checkin.py', 'status_report.py', 'publish_status.py', 'requirements.txt',
                 'docs\index.html', '.github\workflows\daily-checkin.yml')) {
    if (-not (Test-Path $f)) { throw "缺少文件：$f" }
}

Write-Host '[3/6] 配置提交身份（已配置则跳过）...'
if (-not (git config user.name)) {
    git config user.name 'checkin-bot'
    git config user.email 'checkin-bot@users.noreply.github.com'
}

Write-Host '[4/6] 提交改动...'
git add -A
if ($LASTEXITCODE -ne 0) { throw 'git add 失败' }
if ((git status --porcelain)) {
    git commit -m "云端每日自动签到 $(Get-Date -Format 'yyyy-MM-dd HH:mm')" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'git commit 失败' }
    Write-Host '  已提交'
} else {
    Write-Host '  没有新的改动，直接推送'
}

Write-Host "[5/6] 设置远程仓库：$RepoUrl"
$remotes = git remote
if ($remotes -contains 'origin') {
    git remote set-url origin $RepoUrl
} else {
    git remote add origin $RepoUrl
}
if ($LASTEXITCODE -ne 0) { throw '设置远程仓库失败' }

Write-Host "[6/6] 推送到 origin/$Branch ..."
git push -u origin $Branch
if ($LASTEXITCODE -ne 0) {
    throw '推送失败：请确认仓库已创建、地址正确、并已登录 GitHub（git 凭据管理器或 PAT）'
}

Write-Host ''
Write-Host '完成！接下来在 GitHub 网页/手机上做三件事：' -ForegroundColor Green
Write-Host '  1) Settings -> Secrets and variables -> Actions -> 新建 NEWAPI_ACCOUNTS'
Write-Host '     内容 = 本地 .env 里 NEWAPI_ACCOUNTS= 后面那一整段 JSON'
Write-Host '  2) Actions -> 每日自动签到 -> Run workflow（先手动跑一次验证）'
Write-Host '  3) Settings -> Pages -> Source: Deploy from a branch -> main / docs'
Write-Host '     手机打开 https://你的用户名.github.io/仓库名/ 并添加到主屏幕'
Write-Host ''
Write-Host '详细说明见：手机部署指南.md'
