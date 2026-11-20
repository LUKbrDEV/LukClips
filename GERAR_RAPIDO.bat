@echo off
chcp 65001 >nul
cd /d "%~dp0"
title LukClips - gerar so o instalador

rem Versao rapida: NAO instala bibliotecas e NAO faz o zip.
rem Use o GERAR_EXE.bat (completo) na primeira vez ou se der erro de biblioteca.
set "OUT=%LOCALAPPDATA%\LukClips_build"
set "SRC=%~dp0"
set "SRC=%SRC:~0,-1%"

for /f "delims=" %%v in ('python -c "import re,sys;print(re.search('VERSAO = .(.+?).\n',open(sys.argv[1],encoding='utf-8').read()).group(1))" "%SRC%\lukclips.py"') do set "VERSAO=%%v"

echo.
echo  ==========================================
echo    LukClips v%VERSAO% - gerando o instalador
echo  ==========================================
echo.

rem A pasta "build" fica guardada: o PyInstaller reaproveita e fica mais rapido.
if exist "%OUT%\dist" rmdir /s /q "%OUT%\dist"
if not exist "%OUT%" mkdir "%OUT%"

echo [1/3] Montando o programa...
python -m PyInstaller --noconfirm --onedir --windowed --log-level WARN ^
  --name LukClips --icon "%SRC%\lukclips.ico" --add-data "%SRC%\lukclips.png;." ^
  --workpath "%OUT%\build" --distpath "%OUT%\dist" --specpath "%OUT%" ^
  --paths "%SRC%" ^
  --collect-all faster_whisper --collect-all ctranslate2 ^
  --collect-all yt_dlp --collect-all yt_dlp_ejs ^
  --collect-binaries onnxruntime --collect-data cv2 ^
  "%SRC%\lukclips.py"
if errorlevel 1 goto erro

echo.
echo [2/3] Colocando ffmpeg, ffprobe, ffplay e deno junto...
powershell -NoProfile -Command ^
  "foreach($n in 'ffmpeg','ffprobe','ffplay','deno'){ $c=Get-Command $n -ErrorAction SilentlyContinue; if($c){ $p=$c.Source; $i=Get-Item $p; if($i.LinkType){ $p=@($i.Target)[0] }; Copy-Item $p ($env:OUT+'\dist\LukClips\') -Force; Write-Host ('   ok: '+$n) } else { Write-Host ('   AVISO: '+$n+' nao encontrado') -ForegroundColor Yellow } }"

echo.
echo [3/3] Criando LukClips_Setup_%VERSAO%.exe...
set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC (
  echo    Inno Setup nao encontrado. Rode o GERAR_EXE.bat completo uma vez.
  goto erro
)
"%ISCC%" /Q /DVersao=%VERSAO% /DOrigem="%OUT%\dist\LukClips" /O"%OUT%" "%SRC%\LukClips.iss"
if errorlevel 1 goto erro
copy /y "%OUT%\LukClips_Setup_%VERSAO%.exe" "%SRC%\" >nul

echo.
echo  ==========================================
echo    PRONTO!  LukClips_Setup_%VERSAO%.exe
echo    Agora e so rodar o PUBLICAR_VERSAO.bat
echo  ==========================================
echo.
pause
exit /b 0

:erro
echo.
echo  Deu erro. Tire um print desta janela e mande pro Claude.
pause
exit /b 1
