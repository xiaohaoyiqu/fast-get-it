#define MyAppName "欲求达"
#define MyAppVersion "0.2.0"
#define MyAppPublisher "YuqiuDa"
#define MyAppExeName "欲求达.exe"

[Setup]
AppId={{B24C79E1-1F0A-4931-A449-AD5B79027E43}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\YuqiuDa
DefaultGroupName={#MyAppName}
OutputDir=..\dist\installer
OutputBaseFilename=欲求达-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=欲求达.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

[Files]
Source: "..\dist\欲求达\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent
