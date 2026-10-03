@echo off
setlocal

cd /d "%~dp0\.."

echo.
echo ========================================
echo [1/6] Starting Docker environment
echo ========================================

docker compose up -d --build --wait
if errorlevel 1 goto :fail

echo.
echo ========================================
echo [2/6] Setting integration test URLs
echo ========================================

set INCIDENTOPS_TEST_DATABASE_URL=postgresql://incidentops_dev:incidentops_dev_only@127.0.0.1:5433/incidentops_test
set INCIDENTOPS_TEST_INVENTORY_URL=http://127.0.0.1:8001
set INCIDENTOPS_TEST_CHECKOUT_URL=http://127.0.0.1:8002

echo.
echo ========================================
echo [3/6] Running tests
echo ========================================

".venv\Scripts\python.exe" -m pytest -q
if errorlevel 1 goto :fail

echo.
echo ========================================
echo [4/6] Running Ruff
echo ========================================

".venv\Scripts\python.exe" -m ruff check .
if errorlevel 1 goto :fail

".venv\Scripts\python.exe" -m ruff format --check .
if errorlevel 1 goto :fail

echo.
echo ========================================
echo [5/6] Checking working-tree diff
echo ========================================

git diff --check
if errorlevel 1 goto :fail

echo.
echo ========================================
echo [6/6] Checking staged diff
echo ========================================

git diff --cached --check
if errorlevel 1 goto :fail

echo.
echo ========================================
echo VERIFICATION PASSED
echo ========================================

exit /b 0


:fail

echo.
echo ========================================
echo VERIFICATION FAILED
echo ========================================

exit /b 1