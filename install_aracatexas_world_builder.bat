@echo off
setlocal
cd /d "%~dp0"
if not exist "downloads\ara_world_builder_addon.zip" (
  echo Pacote do Araçatexas World Builder nao encontrado.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath 'downloads\ara_world_builder_addon.zip' -DestinationPath '.' -Force"
if errorlevel 1 (
  echo Falha ao extrair o pacote.
  pause
  exit /b 1
)
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Falha ao instalar dependencias Python.
  pause
  exit /b 1
)
python ara_world_app.py
pause
