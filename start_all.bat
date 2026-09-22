@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [1/3] Checking Docker...
docker info >nul 2>&1
if not errorlevel 1 goto docker_ok

echo Docker is not running. Starting Docker Desktop...
if not exist "C:\Program Files\Docker\Docker\Docker Desktop.exe" (
    echo Docker Desktop not found. Please start it manually and retry.
    pause
    exit /b 1
)
start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"

:wait_docker
timeout /t 3 /nobreak >nul
docker info >nul 2>&1
if errorlevel 1 goto wait_docker

:docker_ok
echo [2/3] Starting redis + qdrant...
docker compose up -d redis qdrant

echo [3/3] Starting backend and frontend (each in a new window)...
start "backend :8000" /D "%~dp0backend" cmd /k ".venv\Scripts\python.exe -m uvicorn app.main:app --reload"
start "frontend :5173" /D "%~dp0frontend" cmd /k "npm run dev"

echo.
echo All started:
echo   Backend  http://localhost:8000/health
echo   Frontend http://localhost:5173
echo   (Docker containers run in background; run "docker compose stop" to stop them)
pause
