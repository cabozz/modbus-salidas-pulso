@echo off
echo Compilando modbus_ui.py con PyInstaller...
pyinstaller --onefile --windowed --name "SalidasPulso" modbus_ui.py
echo.
echo Listo. Ejecutable en: dist\SalidasPulso.exe
pause
