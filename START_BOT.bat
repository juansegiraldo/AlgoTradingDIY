@echo off
title 10X Trading Bot - PAPER MODE
color 0A
echo.
echo ============================================================
echo   10X TRADING BOT - PAPER MODE
echo ============================================================
echo.
echo   NO CIERRES ESTA VENTANA - El bot se apaga si la cierras
echo   Para parar: Ctrl+C o cierra la ventana
echo.
echo   Desactivando suspension del PC...
powercfg -change -standby-timeout-ac 0
powercfg -change -hibernate-timeout-ac 0
powercfg -change -monitor-timeout-ac 0
echo   PC no se dormira mientras el bot este activo
echo.
echo ============================================================
echo.

REM Matar instancias anteriores del bot
taskkill /F /IM python.exe >nul 2>&1
timeout /t 2 /noq >nul

cd /d D:\Users\juan.giraldo\Desktop\CodingCamp\AlgoTradingDIY
python main.py

echo.
echo Restaurando configuracion de energia...
powercfg -change -standby-timeout-ac 30
powercfg -change -monitor-timeout-ac 15
echo Bot detenido. Presiona cualquier tecla para cerrar.
pause
