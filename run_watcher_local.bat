@echo off
chcp 65001 >nul
rem ──────────────────────────────────────────────
rem 未発売カードWatcher ローカル実行
rem
rem yu-gi-oh.jp はクラウド事業者のIP（GitHub Actions / Render）を403で
rem 拒否するため、このPC（一般回線）から定期巡回する。
rem タスクスケジューラ「CardSouba-UnreleasedWatcher」から3時間おきに起動される。
rem
rem 接続キーはリポジトリ外の %USERPROFILE%\.cardsouba\watcher.env から読み込む。
rem ログは %USERPROFILE%\.cardsouba\watcher.log に追記される。
rem ──────────────────────────────────────────────
cd /d "%~dp0"

set "ENVFILE=%USERPROFILE%\.cardsouba\watcher.env"
set "LOGFILE=%USERPROFILE%\.cardsouba\watcher.log"

if not exist "%ENVFILE%" (
  echo [%date% %time%] watcher.env が見つかりません: %ENVFILE% >> "%LOGFILE%"
  exit /b 1
)

rem env ファイル（KEY=VALUE 形式、# 始まりはコメント）を環境変数に読み込む
for /f "usebackq eol=# tokens=1,* delims==" %%a in ("%ENVFILE%") do set "%%a=%%b"

rem ホワイトリスト担保テスト（失敗したら巡回せず終了 — クラウド版と同じ安全装置）
python -m pytest tests\test_fetch_guard.py -q >> "%LOGFILE%" 2>&1
if errorlevel 1 (
  echo [%date% %time%] テスト失敗のため巡回を中止 >> "%LOGFILE%"
  exit /b 1
)

echo [%date% %time%] 巡回開始 >> "%LOGFILE%"
python watch_unreleased.py >> "%LOGFILE%" 2>&1
echo [%date% %time%] 巡回終了 ^(exit %errorlevel%^) >> "%LOGFILE%"
