@echo off
title Collatz Conjecture High-Speed Engine
cd /d "%~dp0"
echo ==========================================================
echo  Collatz Conjecture - Automatic Resume Runner
echo ==========================================================
echo.
py collatz_realtime_plotter.py --resume
if errorlevel 1 (
    echo.
    echo [!] Program exited with error.
    pause
)
