; M.Y. Business Hotel HR/Payroll 2.2
; Draft Inno Setup definition. Must be compiled and tested on Windows 10 before release.

#define AppName "M.Y. Business Hotel HR"
#define AppVersion "2.2.0"
#define AppPublisher "M.Y. Business Hotel"
#define SourceRoot AddBackslash(ExtractFileDir(__FILE__)) + ".."

[Setup]
AppId={{A6D9A82B-3D1F-4C90-9E6C-2C9B6D5A22F2}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\MYBusinessHotelHR
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
OutputDir=..\dist
OutputBaseFilename=MYBusinessHotelHR-Setup-2.2.0
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#AppName} {#AppVersion}
Uninstallable=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Dirs]
Name: "{app}"

[Files]
Source: "{#SourceRoot}\app\*"; DestDir: "{app}\app"; Excludes: "__pycache__\*,*.pyc"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceRoot}\alembic\*"; DestDir: "{app}\alembic"; Excludes: "__pycache__\*,*.pyc"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceRoot}\scripts\*"; DestDir: "{app}\scripts"; Excludes: "__pycache__\*,*.pyc"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceRoot}\docs\*"; DestDir: "{app}\docs"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceRoot}\alembic.ini"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\pyproject.toml"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\.env.example"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\USER_GUIDE.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\windows\start.ps1"""; WorkingDir: "{app}"

[Run]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\windows\setup.ps1"""; Description: "Configure Python, PostgreSQL, the local database, and the first administrator"; Flags: waituntilterminated

; Intentionally no UninstallDelete entries: runtime .env, .venv, var data and reports
; must not be recursively removed by an application uninstall or upgrade.
