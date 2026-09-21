@echo off
title Collatz Conjecture High-Speed Engine
cd /d "%~dp0"
echo ==========================================================
echo  Collatz Conjecture High-Speed Runner
echo ==========================================================
echo.
if exist collatz_checkpoint.txt (
    echo [*] Checkpoint found. Resuming from last progress...
    py collatz_realtime_plotter.py --resume
) else (
    echo [*] No checkpoint found. Starting from whole number 1...
    py collatz_realtime_plotter.py --start 1
)
if errorlevel 1 (
    echo.
    echo [!] Program exited with error.
    pause
)
