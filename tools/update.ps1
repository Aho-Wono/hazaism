# ScrapNest を最新版に更新する（update.bat から呼ばれる）。
#   - git で入れた場合（.git がある）: git pull
#   - ZIP で入れた場合              : GitHub から最新の ZIP を取ってきて、プログラム本体を入れ替える
# 設定（config.json）と作業データ（jobs フォルダー）はそのまま残す。
# このファイルは UTF-8（BOM 付き）で保存すること。BOM が無いと Windows PowerShell が日本語を読み違える。
param(
    [string]$Repo = "Aho-Wono/scrapnest",
    [string]$Branch = "main",
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [string]$ZipUrl = ""        # 空なら GitHub の最新版。検査のときだけ別の場所を指定する
)
$ErrorActionPreference = "Stop"

function Get-AppVersion([string]$Dir) {
    $m = Select-String -Path (Join-Path $Dir "pyproject.toml") -Pattern '^\s*version\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($m) { return $m.Matches[0].Groups[1].Value }
    return "?"
}

try {
    $before = Get-AppVersion $Root
    Write-Host "ScrapNest を更新します（いまの版: v$before）"

    # 起動中だと、ライブラリのファイルを入れ替えられないことがある
    $running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match '-m\s+scrapnest' }
    if ($running) { throw "ScrapNest が起動中です。ScrapNest の黒いウィンドウを閉じてから、もう一度実行してください。" }

    $git = Get-Command git -ErrorAction SilentlyContinue
    if ((Test-Path (Join-Path $Root ".git")) -and $git) {
        Write-Host "git で最新版を取得しています..."
        & git -C $Root pull --ff-only
        if ($LASTEXITCODE -ne 0) { throw "git pull に失敗しました（ファイルを書き換えている場合は、元に戻してからやり直してください）。" }
    }
    else {
        Write-Host "最新版をダウンロードしています..."
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        $ProgressPreference = "SilentlyContinue"
        $tmp = Join-Path ([IO.Path]::GetTempPath()) ("scrapnest_update_" + [Guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $tmp | Out-Null
        try {
            $zip = Join-Path $tmp "scrapnest.zip"
            if (-not $ZipUrl) { $ZipUrl = "https://github.com/$Repo/archive/refs/heads/$Branch.zip" }
            Invoke-WebRequest -Uri $ZipUrl -OutFile $zip -UseBasicParsing
            Expand-Archive -Path $zip -DestinationPath $tmp -Force
            $src = Get-ChildItem -Path $tmp -Directory | Select-Object -First 1
            if (-not $src -or -not (Test-Path (Join-Path $src.FullName "pyproject.toml"))) {
                throw "ダウンロードした中身が想定と違います。"
            }
            # プログラム本体のフォルダーは丸ごと入れ替える（古いファイルを残さない）
            foreach ($d in "scrapnest", "tests", "tools") {
                $from = Join-Path $src.FullName $d
                if (Test-Path $from) {
                    $to = Join-Path $Root $d
                    if (Test-Path $to) { Remove-Item -Path $to -Recurse -Force }
                    Copy-Item -Path $from -Destination $to -Recurse -Force
                }
            }
            # 直下のファイル（run.bat、README など）は上書きする。config.json と jobs は配布物に無いので残る
            Get-ChildItem -Path $src.FullName -File -Force | ForEach-Object {
                Copy-Item -Path $_.FullName -Destination (Join-Path $Root $_.Name) -Force
            }
        }
        finally {
            Remove-Item -Path $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    # ライブラリをそろえる
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        $env:PATH = (Join-Path $env:USERPROFILE ".local\bin") + ";" + $env:PATH
    }
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Write-Host "ライブラリを確認しています..."
        Push-Location $Root
        try {
            & uv sync --no-dev --inexact
            if ($LASTEXITCODE -ne 0) { throw "ライブラリの更新に失敗しました。" }
        }
        finally { Pop-Location }
    }

    $after = Get-AppVersion $Root
    Write-Host ""
    if ($after -eq $before) { Write-Host "すでに最新版です（v$after）。" -ForegroundColor Green }
    else { Write-Host "更新しました: v$before → v$after" -ForegroundColor Green }
    Write-Host "run.bat で起動してください。"
    exit 0
}
catch {
    Write-Host ""
    Write-Host ("更新できませんでした: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
