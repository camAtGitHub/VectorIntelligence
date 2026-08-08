@echo off
title Vector AI - state dump
REM Thin launcher for dump-vector-ai-state.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dump-vector-ai-state.ps1" %*
exit /b %ERRORLEVEL%
