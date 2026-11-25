; Optional desktop preparation candidate, not a tested Windows release.
; Build only from reviewed source, do not distribute until Windows acceptance.
#define AppName "HOnY"
#define SourceRoot AddBackslash(ExtractFileDir(__FILE__)) + ".."
[Setup]
AppId={{EC67865F-C7A7-4E77-A35B-B7554D8F4A81}
AppName={#AppName} desktop candidate
AppVersion=2.2.0
DefaultDirName={autopf}\HOnY
DefaultGroupName={#AppName}
PrivilegesRequired=admin
OutputDir=..\dist
OutputBaseFilename=HOnY-Desktop-Candidate-2.2.0
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\app\static\hony.ico
UninstallDisplayIcon={app}\app\static\hony.ico
[Files]
Source: "{#SourceRoot}\app\*"; DestDir: "{app}\app"; Excludes: "__pycache__\*,*.pyc"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceRoot}\alembic\*"; DestDir: "{app}\alembic"; Excludes: "__pycache__\*,*.pyc"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceRoot}\scripts\*"; DestDir: "{app}\scripts"; Excludes: "__pycache__\*,*.pyc"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceRoot}\docs\*"; DestDir: "{app}\docs"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceRoot}\pyproject.toml"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\alembic.ini"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\.env.example"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\USER_GUIDE.md"; DestDir: "{app}"; Flags: ignoreversion
[Icons]
Name: "{group}\HOnY"; Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: "-I ""{app}\scripts\windows\desktop_window_host.py"""; WorkingDir: "{app}"; IconFilename: "{app}\app\static\hony.ico"
Name: "{autodesktop}\HOnY"; Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: "-I ""{app}\scripts\windows\desktop_window_host.py"""; WorkingDir: "{app}"; IconFilename: "{app}\app\static\hony.ico"
; No [Run]: staging files must not install dependencies, touch a database,
; register/start a service, issue certificates or change network policy.
; No recursive runtime deletion on uninstall.
[Code]
function InitializeSetup(): Boolean;
var
  ExitCode: Integer;
begin
  Result := False;
  if Exec(ExpandConstant('{cmd}'), '/C sc.exe query HOnYBackend', '', SW_HIDE, ewWaitUntilTerminated, ExitCode) then
    if ExitCode = 1060 then
      Result := True
    else
      MsgBox('Existing or unverifiable HOnY service state. Use a reviewed stopped-service upgrade procedure before replacing files.', mbError, MB_OK);
end;

function InitializeUninstall(): Boolean;
var
  ExitCode: Integer;
begin
  Result := False;
  if Exec(ExpandConstant('{cmd}'), '/C sc.exe query HOnYBackend', '', SW_HIDE, ewWaitUntilTerminated, ExitCode) then
    if ExitCode = 0 then
    begin
      MsgBox('Remove the HOnYBackend service with the reviewed removal script before uninstalling application files. Data and certificates must be preserved.', mbError, MB_OK);
      Result := False;
    end
    else if ExitCode = 1060 then
      Result := True
    else
      MsgBox('Service state could not be verified. Application files were preserved.', mbError, MB_OK);
end;
