@echo off
title Vector - Stop
REM Self-contained - stops the VectorPod-Supervisor scheduled task.
REM The supervisor's job object terminates chipper + vector-ai with it,
REM so stopping the one task cleanly takes down the whole stack.

echo Stopping Vector...
schtasks /end /tn "VectorPod-Supervisor" >nul 2>&1

echo [+] Stopped. Stack down.
echo     Start again with start-vector.cmd.
timeout /t 4 >nul
