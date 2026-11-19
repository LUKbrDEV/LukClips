; LukClips - instalador (Inno Setup 6)
; Feito por LukDev. Gerado pelo GERAR_EXE.bat; não precisa abrir este arquivo.
#ifndef Versao
  #define Versao "3.0"
#endif
#ifndef Origem
  #define Origem GetEnv("LOCALAPPDATA") + "\LukClips_build\dist\LukClips"
#endif

[Setup]
AppId={{8C1F7A52-5E1B-4C47-9C3E-4C1A1D2B7E10}
AppName=LukClips
AppVersion={#Versao}
AppVerName=LukClips {#Versao}
AppPublisher=LukDev
DefaultDirName={localappdata}\Programs\LukClips
DefaultGroupName=LukClips
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputBaseFilename=LukClips_Setup_{#Versao}
SetupIconFile=lukclips.ico
UninstallDisplayIcon={app}\LukClips.exe
UninstallDisplayName=LukClips
Compression=lzma2
SolidCompression=no
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "pt"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "atalho"; Description: "Criar atalho na área de trabalho"; GroupDescription: "Atalhos:"

[Files]
Source: "{#Origem}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\LukClips"; Filename: "{app}\LukClips.exe"
Name: "{autodesktop}\LukClips"; Filename: "{app}\LukClips.exe"; Tasks: atalho

[Run]
Filename: "{app}\LukClips.exe"; Description: "Abrir o LukClips agora"; Flags: nowait postinstall skipifsilent
Filename: "{app}\LukClips.exe"; Flags: nowait; Check: WizardSilent
