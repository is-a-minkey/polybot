@echo off
rem Rebuilds polybot.exe from polybot.py on Windows. Needs Python 3.10+ (python.org) on PATH.
python -m venv .venv || goto :err
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip pyinstaller httpx py-clob-client-v2 || goto :err
pyinstaller --onefile --noconfirm --clean --name polybot --console --hidden-import py_clob_client_v2 --exclude-module pytest --exclude-module _pytest --exclude-module tkinter polybot.py || goto :err
echo.
dist\polybot.exe selfcheck
echo.
echo Done: dist\polybot.exe
goto :eof
:err
echo Build failed.
exit /b 1
