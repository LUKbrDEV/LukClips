@echo off
chcp 65001 >nul
cd /d "%~dp0"
title LukClips - gerando o programa

rem Monta fora do OneDrive (o OneDrive trava os arquivos temporarios)
set "OUT=%LOCALAPPDATA%\LukClips_build"
set "SRC=%~dp0"
set "SRC=%SRC:~0,-1%"

for /f "delims=" %%v in ('python -c "import re,sys;print(re.search('VERSAO = .(.+?).\n',open(sys.argv[1],encoding='utf-8').read()).group(1))" "%SRC%\lukclips.py"') do set "VERSAO=%%v"

echo.
echo  ==========================================
echo    LukClips v%VERSAO% - gerando o programa
echo  ==========================================
echo.

if exist "%SRC%\build" rmdir /s /q "%SRC%\build" 2>nul
if exist "%SRC%\dist" rmdir /s /q "%SRC%\dist" 2>nul
if exist "%SRC%\LukClips.spec" del /q "%SRC%\LukClips.spec" 2>nul
if exist "%OUT%" rmdir /s /q "%OUT%"
mkdir "%OUT%"

echo [1/5] Instalando as bibliotecas...
python -m pip install -U pip >nul
python -m pip install -U pyinstaller "yt-dlp[default]" faster-whisper "opencv-python<5" numpy
if errorlevel 1 goto erro

echo.
echo [2/5] Montando o programa (demora alguns minutos)...
python -m PyInstaller --noconfirm --onedir --windowed ^
  --name LukClips --icon "%SRC%\lukclips.ico" --add-data "%SRC%\lukclips.png;." ^
  --workpath "%OUT%\build" --distpath "%OUT%\dist" --specpath "%OUT%" ^
  --paths "%SRC%" ^
  --collect-all faster_whisper --collect-all ctranslate2 ^
  --collect-all yt_dlp --collect-all yt_dlp_ejs ^
  --collect-binaries onnxruntime --collect-data cv2 ^
  "%SRC%\lukclips.py"
if errorlevel 1 goto erro

echo.
echo [3/5] Colocando ffmpeg, ffprobe, ffplay e deno junto do programa...
powershell -NoProfile -Command ^
  "foreach($n in 'ffmpeg','ffprobe','ffplay','deno'){ $c=Get-Command $n -ErrorAction SilentlyContinue; if($c){ $p=$c.Source; $i=Get-Item $p; if($i.LinkType){ $p=@($i.Target)[0] }; Copy-Item $p ($env:OUT+'\dist\LukClips\') -Force; Write-Host ('   ok: '+$n) } else { Write-Host ('   AVISO: '+$n+' nao encontrado') -ForegroundColor Yellow } }"

echo.
echo [4/5] Criando o instalador LukClips_Setup_%VERSAO%.exe...
set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC (
  echo    Inno Setup nao encontrado. Instalando pelo winget...
  winget install -e --id JRSoftware.InnoSetup --accept-source-agreements --accept-package-agreements
  if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
  if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
)
if defined ISCC (
  "%ISCC%" /Q /DVersao=%VERSAO% /DOrigem="%OUT%\dist\LukClips" /O"%OUT%" "%SRC%\LukClips.iss"
  if errorlevel 1 goto erro
  copy /y "%OUT%\LukClips_Setup_%VERSAO%.exe" "%SRC%\" >nul
  echo    ok: LukClips_Setup_%VERSAO%.exe
) else (
  echo    AVISO: nao deu para instalar o Inno Setup. Pulei o instalador; o zip continua funcionando.
)

echo.
echo [5/5] Compactando LukClips.zip (versao sem instalar)...
if exist "%SRC%\LukClips.zip" del /q "%SRC%\LukClips.zip"
powershell -NoProfile -Command "Compress-Archive -Path ($env:OUT+'\dist\LukClips') -DestinationPath ($env:SRC+'\LukClips.zip') -Force"
if errorlevel 1 goto erro

echo.
echo  ==========================================
echo    PRONTO!  (v%VERSAO%)
echo    Instalador para os amigos:  LukClips_Setup_%VERSAO%.exe
echo    Versao sem instalar:        LukClips.zip
echo  ==========================================
echo.
explorer "%SRC%"
pause
exit /b 0

:erro
echo.
echo  Deu erro. Tire um print desta janela e mande pro Claude.
pause
exit /b 1
