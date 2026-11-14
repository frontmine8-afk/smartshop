@echo off
chcp 65001 >nul
title SMART SHOP
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
  set PY=py
) else (
  set PY=python
)

if not exist .venv (
  echo [1/3] Virtual muhit yaratilmoqda...
  %PY% -m venv .venv
  if errorlevel 1 goto error
)

echo [2/3] Kutubxonalar o'rnatilmoqda...
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto error

if not exist .env (
  copy .env.example .env >nul
  echo .env yaratildi. Telegram bot uchun BOT_TOKEN va ADMIN_CHAT_IDS ni kiriting.
)

echo [3/3] Server ishga tushmoqda...
echo Sayt: http://127.0.0.1:8000
echo Admin: http://127.0.0.1:8000/admin
echo To'xtatish uchun CTRL+C bosing.
.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
pause
exit /b 0

:error
echo Xatolik yuz berdi. Python 3.10+ o'rnatilganini va internetni tekshiring.
pause
exit /b 1
