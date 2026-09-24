@echo off
:: ============================================================
::  KGP Crew Monitoring System — Windows Dev Startup
::  Usage: start_dev.bat
:: ============================================================

SET FLASK_ENV=development
SET PORT=5400

echo [CMS] Starting development server on http://localhost:%PORT% ...
cd /d "%~dp0"
call venv\Scripts\activate.bat
python -m webapp.app
