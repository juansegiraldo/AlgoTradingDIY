@echo off
title 10X Dashboard
color 0B
echo.
echo ============================================================
echo   10X TRADING DASHBOARD
echo ============================================================
echo.
echo   Abriendo en http://localhost:8501
echo   NO CIERRES ESTA VENTANA
echo.
echo ============================================================
echo.
cd /d D:\Users\juan.giraldo\Desktop\CodingCamp\AlgoTradingDIY
start http://localhost:8501
python -m streamlit run dashboard/app.py --server.port 8501 --server.headless true
pause
