@echo off
:: ============================================================
::  KGP Crew Monitoring System — Windows Dev Startup
::  Usage: start_dev.bat
:: ============================================================

SET FLASK_ENV=development
SET PORT=5000

echo [CMS] Starting development server on http://localhost:%PORT% ...
cd /d "%~dp0"
call venv\Scripts\activate.bat
python webapp\app.py
