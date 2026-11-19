@echo off
chcp 65001 >nul
cd /d "%~dp0"
title LukClips - publicar nova versao
set "SRC=%~dp0"
set "SRC=%SRC:~0,-1%"

for /f "delims=" %%v in ('python -c "import re,sys;print(re.search('VERSAO = .(.+?).\n',open(sys.argv[1],encoding='utf-8').read()).group(1))" "%SRC%\lukclips.py"') do set "VERSAO=%%v"
for /f "delims=" %%r in ('python -c "import re,sys;print(re.search('REPO_GITHUB = .(.*?).\n',open(sys.argv[1],encoding='utf-8').read()).group(1))" "%SRC%\lukclips.py"') do set "REPO=%%r"

if "%REPO%"=="" (
  echo.
  echo  REPO_GITHUB esta vazio no lukclips.py.
  echo  1. Crie um repositorio no GitHub, ex.: seu-usuario/LukClips
  echo  2. No lukclips.py, preencha:  REPO_GITHUB = "seu-usuario/LukClips"
  echo  3. Rode o GERAR_EXE.bat e depois este arquivo de novo.
  pause
  exit /b 1
)
if not exist "%SRC%\LukClips_Setup_%VERSAO%.exe" (
  echo  Nao achei LukClips_Setup_%VERSAO%.exe. Rode o GERAR_EXE.bat primeiro.
  pause
  exit /b 1
)
where gh >nul 2>nul
if errorlevel 1 (
  echo  Instalando o GitHub CLI...
  winget install -e --id GitHub.cli --accept-source-agreements --accept-package-agreements
  echo  Feche esta janela, abra de novo e rode outra vez.
  pause
  exit /b 1
)
gh auth status >nul 2>nul
if errorlevel 1 gh auth login

set /p NOTAS=O que mudou nesta versao? (uma linha): 
gh release create v%VERSAO% "%SRC%\LukClips_Setup_%VERSAO%.exe" -R %REPO% --title "LukClips v%VERSAO%" --notes "%NOTAS%"
if errorlevel 1 (
  echo  Deu erro ao publicar. Tire um print e mande pro Claude.
  pause
  exit /b 1
)
echo.
echo  Publicado! Quem tiver o LukClips instalado vai receber o aviso de atualizacao.
pause
