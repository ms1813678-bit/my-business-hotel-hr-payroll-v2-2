; Staged installer recipe. Consumes the tree written by installer\staging\hony_stage.py.
; NOT COMPILED, NOT PARSED, NOT RUN, NOT PROVEN ON WINDOWS. Written in a Linux sandbox with no Inno Setup.
; The Pascal below is checked by tests for real section structure (section headers in order,
; Pascal only inside [Code]) and for text contracts; that still does NOT prove it compiles or
; behaves as written. No Inno compiler or Inno-compatible parser was available in the sandbox.
; Compile only with: ISCC /DStageRoot=<staging>\inno HOnY-Desktop-Staged.iss  (after verify-tree passes)
; Needs Inno Setup 6.x (uses GetSHA256OfFile).
;
; SAFETY POSTURE OF THIS SCAFFOLD: it performs NO automatic destructive compensation at all.
; No DelTree, no vendor uninstall, no cleanup of any kind. On failure it records leftovers in
; the journal, report-only, for reviewed manual handling. It is only temporarily safe because
; it is inert; this does NOT waive the final product's rollback requirement (a reviewed native
; ownership helper with exclusive identity, reparse protection and real fault-injection tests).
; This scaffold is not whole-system atomic: there is no compensation to make atomic, only a report.
#ifndef StageRoot
  #error "Pass /DStageRoot=<staging tree>\inno"
#endif
#include AddBackslash(StageRoot) + "staged-inputs.iss.inc"
#ifndef HasPython
  #error "Staged tree has no pinned Python installer."
#endif
#define AppName "HOnY"
[Setup]
AppId={{EC67865F-C7A7-4E77-A35B-B7554D8F4A81}
AppName={#AppName} desktop
AppVersion={#StagedVersion}
DefaultDirName={autopf}\HOnY
DefaultGroupName={#AppName}
PrivilegesRequired=admin
OutputDir=..\dist
OutputBaseFilename=HOnY-Desktop-Staged-{#StagedVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile={#StageRoot}\app-src\app\static\hony.ico
UninstallDisplayIcon={app}\app\static\hony.ico
[Files]
Source: "{#StageRoot}\app-src\app\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#StageRoot}\app-src\alembic\*"; DestDir: "{app}\alembic"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#StageRoot}\app-src\scripts\*"; DestDir: "{app}\scripts"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#StageRoot}\app-src\docs\*"; DestDir: "{app}\docs"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#StageRoot}\app-src\pyproject.toml"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#StageRoot}\app-src\alembic.ini"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#StageRoot}\app-src\.env.example"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#StageRoot}\app-src\README.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#StageRoot}\app-src\USER_GUIDE.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#StageRoot}\wheelhouse\*"; DestDir: "{app}\wheelhouse"; Flags: ignoreversion
Source: "{#StageRoot}\requirements-locked.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#StageRoot}\installer-checks\*"; DestDir: "{app}\installer-checks"; Flags: ignoreversion
Source: "{#StageRoot}\inputs\{#PythonFile}"; DestDir: "{tmp}"; Flags: deleteafterinstall
#ifdef HasWebview2
Source: "{#StageRoot}\inputs\{#Webview2File}"; DestDir: "{tmp}"; Flags: deleteafterinstall
#endif
#ifdef HasPostgresql
Source: "{#StageRoot}\inputs\{#PostgresqlFile}"; DestDir: "{tmp}"; Flags: deleteafterinstall
#endif
[Icons]
Name: "{group}\HOnY"; Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: "-I ""{app}\scripts\windows\desktop_window_host.py"""; WorkingDir: "{app}"; IconFilename: "{app}\app\static\hony.ico"
Name: "{autodesktop}\HOnY"; Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: "-I ""{app}\scripts\windows\desktop_window_host.py"""; WorkingDir: "{app}"; IconFilename: "{app}\app\static\hony.ico"
; No [Run] on purpose: every step is journaled, hash-checked and exit-code-checked in [Code].
[Code]
const
  { Component states. Absence before the run is never ownership. ST_OK only means this
    run observed a successful install; in this unproven scaffold it authorizes the
    ownership record, never any automatic removal. }
  ST_UNKNOWN = 0;   { detection could not decide: rejected before extraction, never installed over, never removed }
  ST_PRE     = 1;   { found before the run: never touched }
  ST_PLANNED = 2;   { absent before the run, nothing run yet }
  ST_STARTED = 3;   { installer launched; outcome not proven: never removed automatically }
  ST_OK      = 4;   { proven successful new install by this run }

var
  PyState, Wv2State, PgState, VenvState: Integer;
  JournalOk: Boolean;
  EffectsStarted: Boolean;
  CurrentStep: String;

{ ---- journal: write failure is fatal before effects and blocks the leftover report ---- }
function StateDir(): String;
begin
  Result := ExpandConstant('{commonappdata}\HOnY\install-state');
end;

function JournalPath(): String;
begin
  Result := ExpandConstant('{app}\installer-checks\install-journal.jsonl');
end;

function JStr(const S: String): String;
var
  I: Integer;
  C: Char;
begin
  Result := '';
  for I := 1 to Length(S) do
  begin
    C := S[I];
    if C = '\' then Result := Result + '\\'
    else if C = '"' then Result := Result + '\"'
    else Result := Result + C;
  end;
end;

{ Returns False and latches JournalOk := False on any write failure. Append only;
  SaveStringToFile gives no fsync guarantee, so a power loss can still lose the tail. }
function JEvent(const Ev, Pairs: String): Boolean;
begin
  Result := False;
  if not JournalOk then Exit;
  try
    ForceDirectories(ExtractFileDir(JournalPath()));
    Result := SaveStringToFile(JournalPath(), '{"schema":1,"event":"' + Ev + '"' + Pairs + '}' + #13#10, True);
  except
    Result := False;
  end;
  if not Result then JournalOk := False;
end;

{ Write-ahead use: raises, so the effect after it never runs without its journal line. }
procedure JEventStrict(const Ev, Pairs: String);
begin
  if not JEvent(Ev, Pairs) then
    RaiseException('Install journal could not be written (' + Ev + '). Install stopped before the next change.');
end;

function StateName(St: Integer): String;
begin
  case St of
    ST_UNKNOWN: Result := 'unknown';
    ST_PRE: Result := 'pre-existing';
    ST_PLANNED: Result := 'planned';
    ST_STARTED: Result := 'started-unproven';
    ST_OK: Result := 'ok-proven';
  else
    Result := 'unknown';
  end;
end;

procedure JComponentState(const Comp: String; St: Integer);
begin
  JEventStrict('component-state', ',"component":"' + Comp + '","state":"' + StateName(St) + '"');
end;

procedure JOwned(const Comp: String);
begin
  JEventStrict('ownership', ',"component":"' + Comp + '","ownership":"installed-by-this-installer"');
end;

procedure JStepOk(const Id: String);
begin
  JEventStrict('step-ok', ',"id":"' + Id + '"');
end;

function RunChecked(const Exe, Params, WorkDir: String; const OkCodes: array of Integer; const What: String): Boolean;
var
  Code, I: Integer;
begin
  Result := False;
  if not Exec(Exe, Params, WorkDir, SW_HIDE, ewWaitUntilTerminated, Code) then
    RaiseException(What + ' could not start (error ' + IntToStr(Code) + ').');
  for I := 0 to GetArrayLength(OkCodes) - 1 do
    if Code = OkCodes[I] then Result := True;
  if not Result then
    RaiseException(What + ' failed with exit code ' + IntToStr(Code) + '. Install stopped. Nothing after this step was run.');
end;

procedure CheckHash(const Name, Expected: String);
begin
  if CompareText(GetSHA256OfFile(ExpandConstant('{tmp}\' + Name)), Expected) <> 0 then
    RaiseException(Name + ' fingerprint does not match the pinned value. Install stopped before running it.');
end;

{ ---- detection ---- }
function WebView2Present(): Boolean;
var
  V: String;
begin
  Result := False;
  if RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', V) then
    Result := (V <> '') and (V <> '0.0.0.0');
end;

{ Direct service scan with clear exit states, no shell pipe: sc's own exit code decides
  whether the scan ran at all, and the output is parsed here in Pascal. Any failure
  is ST_UNKNOWN, which the caller rejects; "absent" is only claimed from a successful scan. }
function PgDetect(): Integer;
var
  ExitCode, I: Integer;
  OutFile: String;
  Lines: TArrayOfString;
begin
  Result := ST_UNKNOWN;
  OutFile := ExpandConstant('{tmp}\hony-sc-services.txt');
  if not Exec(ExpandConstant('{cmd}'), '/C sc.exe query type= service state= all > "' + OutFile + '"', '', SW_HIDE, ewWaitUntilTerminated, ExitCode) then
    Exit;  { sc could not be launched: unknown }
  if ExitCode <> 0 then
    Exit;  { sc itself failed: unknown, never reported as absent }
  if not LoadStringsFromFile(OutFile, Lines) then
  begin
    DeleteFile(OutFile);
    Exit;  { scan output unreadable: unknown }
  end;
  DeleteFile(OutFile);
  Result := ST_PLANNED;
  for I := 0 to GetArrayLength(Lines) - 1 do
    if Pos('postgresql', Lowercase(Lines[I])) > 0 then
    begin
      Result := ST_PRE;
      Exit;
    end;
end;

{ ---- leftover report: report-only, NO automatic destructive compensation ----
       This unproven scaffold never runs DelTree and never runs a vendor uninstaller.
       Every effect this run may have caused is journaled as left behind, with manual
       instructions, for reviewed human handling. Nothing here is atomic. }
procedure RecordLeftovers(const FailedStep: String);
begin
  if not JournalOk then Exit;  { fail closed: if we cannot record, we say so and stop }
  JEvent('rollback-start', ',"failed_step":"' + JStr(FailedStep) + '","mode":"report-only"');
  if (VenvState = ST_STARTED) or (VenvState = ST_OK) then
    JEvent('rollback-left-behind', ',"kind":"dir","path":"' + JStr(ExpandConstant('{app}\.venv')) +
      '","reason":"possibly created by this run (state ' + StateName(VenvState) + '); no automatic removal in this scaffold; remove manually only after identity review"');
  if (PyState = ST_STARTED) or (PyState = ST_OK) then
    JEvent('rollback-left-behind', ',"component":"python","reason":"vendor installer ran (state ' + StateName(PyState) +
      '); no automatic uninstall in this scaffold; if a reviewed check proves this run installed it, remove Python through Windows Apps manually"')
  else if PyState <> ST_PLANNED then
    JEvent('rollback-left-behind', ',"component":"python","reason":"state ' + StateName(PyState) + ': not installed by this run; untouched"');
  JEvent('rollback-left-behind', ',"component":"webview2","reason":"state ' + StateName(Wv2State) + '; shared machine-wide runtime, never removed automatically"');
  JEvent('rollback-left-behind', ',"component":"postgresql","reason":"state ' + StateName(PgState) + '; never removed automatically"');
  JEvent('rollback-left-behind', ',"kind":"machine-state","reason":"vendor installers may leave registry and other machine state; not reversed"');
  JEvent('rollback-left-behind', ',"kind":"files","reason":"files extracted by [Files] before the failure are left in place; Inno''s own extraction cleanup is unverified here"');
  JEvent('rollback-done', ',"mode":"report-only"');
end;

{ First gate, before the wizard and before any file extraction. Fail closed on every
  outcome that is not a proven "service does not exist". }
function InitializeSetup(): Boolean;
var
  ExitCode: Integer;
begin
  Result := False;
  if not Exec(ExpandConstant('{cmd}'), '/C sc.exe query HOnYBackend', '', SW_HIDE, ewWaitUntilTerminated, ExitCode) then
  begin
    MsgBox('The HOnYBackend service state could not be checked (sc.exe did not run). Install stopped before any file was extracted.', mbError, MB_OK);
    Exit;
  end;
  if ExitCode = 1060 then
    Result := True  { service does not exist: clear to proceed }
  else if ExitCode = 0 then
    MsgBox('An existing HOnYBackend service was found. Use a reviewed stopped-service upgrade procedure before replacing files. Install stopped before any file was extracted.', mbError, MB_OK)
  else
    MsgBox('HOnY service state is unverifiable (sc exit code ' + IntToStr(ExitCode) + '). This build rejects unknown state. Install stopped before any file was extracted.', mbError, MB_OK);
end;

{ Second gate: runs after the wizard, still before a single file is extracted. Returning a
  non-empty string aborts the install with zero machine changes. All detection and all
  rejections live here, so nothing in ssPostInstall can be the first rejection point. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Venv, PyExe: String;
begin
  Result := '';
  Venv := ExpandConstant('{app}\.venv');
  PyExe := ExpandConstant('{autopf}\HOnY\python\python.exe');
  if DirectoryExists(Venv) or FileExists(Venv) then
  begin
    Result := 'A .venv already exists at ' + Venv + '. This installer never reuses or replaces one. Stopped before any file was extracted.';
    Exit;
  end;
  VenvState := ST_PLANNED;
  if FileExists(PyExe) then PyState := ST_PRE else PyState := ST_PLANNED;
  if WebView2Present() then Wv2State := ST_PRE else Wv2State := ST_PLANNED;
#ifndef HasWebview2
  if Wv2State <> ST_PRE then
  begin
    Result := 'WebView2 runtime is not installed and this staged build does not carry it. Install it separately, then re-run. Stopped before any file was extracted.';
    Exit;
  end;
#endif
#ifdef HasPostgresql
  PgState := PgDetect();
  if PgState = ST_UNKNOWN then
  begin
    Result := 'PostgreSQL presence could not be determined (service scan failed or was refused). This build rejects unknown state rather than guessing. Stopped before any file was extracted.';
    Exit;
  end;
#else
  PgState := ST_UNKNOWN;  { not carried by this build: never installed, never removed }
#endif
end;

function OwnName(St: Integer; const PreName: String): String;
begin
  if St = ST_OK then Result := 'installed-by-this-installer'
  else if St = ST_PRE then Result := PreName
  else Result := 'unknown';
end;

function WriteMarker(const Comp: String): Boolean;
begin
  Result := False;
  try
    ForceDirectories(StateDir());
    Result := SaveStringToFile(StateDir() + '\' + Comp + '.owned-by-hony', 'installed-by-this-installer' + #13#10, False);
  except
    Result := False;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  PyDir, PyExe, Venv, Msg, OJson: String;
  RecordOk: Boolean;
begin
  if CurStep <> ssPostInstall then Exit;
  JournalOk := True;
  EffectsStarted := False;
  PyDir := ExpandConstant('{autopf}\HOnY\python');
  PyExe := PyDir + '\python.exe';
  Venv := ExpandConstant('{app}\.venv');
  CurrentStep := 'journal-open';
  try
    JEventStrict('journal-open', ',"version":"{#StagedVersion}"');
    { Component states were detected and all rejections happened in PrepareToInstall,
      before file extraction. Here we only journal them and run the planned effects. }
    if VenvState <> ST_PLANNED then
      RaiseException('Internal state lost: the venv check from PrepareToInstall is missing. Stopped before runtime effects.');
    JComponentState('python', PyState);
    JComponentState('webview2', Wv2State);
    JComponentState('postgresql', PgState);
    { ---- effects. Every step: hash first, step-start journaled before launch, state moves
         to STARTED at launch and to OK only after exit code and an observed result. ---- }
    if PyState = ST_PLANNED then
    begin
      CurrentStep := 'install-python';
      CheckHash('{#PythonFile}', '{#PythonSha256}');
      JEventStrict('step-start', ',"id":"install-python"');
      EffectsStarted := True;
      PyState := ST_STARTED;
      RunChecked(ExpandConstant('{tmp}\{#PythonFile}'), '{#PythonArgs} TargetDir="' + PyDir + '"', '', [0], 'Python installer');
      if not FileExists(PyExe) then RaiseException('Python not found at ' + PyExe + ' after install.');
      PyState := ST_OK;
      JOwned('python');
      JStepOk('install-python');
    end;
#ifdef HasWebview2
    if Wv2State = ST_PLANNED then
    begin
      CurrentStep := 'install-webview2';
      CheckHash('{#Webview2File}', '{#Webview2Sha256}');
      JEventStrict('step-start', ',"id":"install-webview2"');
      EffectsStarted := True;
      Wv2State := ST_STARTED;
      RunChecked(ExpandConstant('{tmp}\{#Webview2File}'), '{#Webview2Args}', '', [0], 'WebView2 installer');
      if not WebView2Present() then RaiseException('WebView2 runtime not detected after install.');
      Wv2State := ST_OK;
      JOwned('webview2');
      JStepOk('install-webview2');
    end;
#endif
#ifdef HasPostgresql
    if PgState = ST_PLANNED then
    begin
      CurrentStep := 'install-postgresql';
      CheckHash('{#PostgresqlFile}', '{#PostgresqlSha256}');
      JEventStrict('step-start', ',"id":"install-postgresql"');
      EffectsStarted := True;
      PgState := ST_STARTED;
      RunChecked(ExpandConstant('{tmp}\{#PostgresqlFile}'), '{#PostgresqlArgs}', '', [0], 'PostgreSQL installer');
      if PgDetect() <> ST_PRE then RaiseException('PostgreSQL service not detected after install.');
      PgState := ST_OK;
      JOwned('postgresql');
      JStepOk('install-postgresql');
    end;
#endif
    CurrentStep := 'create-venv';
    JEventStrict('step-start', ',"id":"create-venv"');
    EffectsStarted := True;
    VenvState := ST_STARTED;
    RunChecked(PyExe, '-m venv "' + Venv + '"', ExpandConstant('{app}'), [0], 'venv creation');
    VenvState := ST_OK;
    JEventStrict('created', ',"step":"create-venv","kind":"dir","path":"' + JStr(Venv) + '"');
    JStepOk('create-venv');
    CurrentStep := 'install-wheels';
    JEventStrict('step-start', ',"id":"install-wheels"');
    RunChecked(Venv + '\Scripts\python.exe', '-m pip install --no-index --no-deps --require-hashes --find-links "' + ExpandConstant('{app}\wheelhouse') + '" -r "' + ExpandConstant('{app}\requirements-locked.txt') + '"', ExpandConstant('{app}'), [0], 'locked wheel install');
    JStepOk('install-wheels');
    CurrentStep := 'verify-install';
    JEventStrict('step-start', ',"id":"verify-install"');
    RunChecked(Venv + '\Scripts\python.exe', '-I "' + ExpandConstant('{app}\installer-checks\verify_install.py') + '" "' + ExpandConstant('{app}\installer-checks\wheels.lock.json') + '"', ExpandConstant('{app}'), [0], 'post-install verification');
    JStepOk('verify-install');
  except
    Msg := GetExceptionMessage();
    JEvent('step-failed', ',"id":"' + JStr(CurrentStep) + '"');
    if EffectsStarted then
    begin
      RecordLeftovers(CurrentStep);
      RaiseException(Msg + #13#10 + 'This unproven scaffold performed NO automatic removal. Leftovers were recorded, report-only, for manual review.' +
        ' Journal: ' + JournalPath());
    end
    else
    begin
      JEvent('preflight-failed', ',"id":"' + JStr(CurrentStep) + '"');
      RaiseException(Msg);
    end;
  end;
  { Success: record ownership. A failed record write is reported, never "fixed" by removing anything. }
  RecordOk := True;
  if PyState = ST_OK then RecordOk := WriteMarker('python') and RecordOk;
  if Wv2State = ST_OK then RecordOk := WriteMarker('webview2') and RecordOk;
#ifdef HasPostgresql
  if PgState = ST_OK then RecordOk := WriteMarker('postgresql') and RecordOk;
#endif
  OJson := '{"schema":1,"components":{' +
    '"python":{"ownership":"' + OwnName(PyState, 'pre-existing') + '","evidence":{"path":"' + JStr(PyExe) + '"}},' +
    '"webview2":{"ownership":"' + OwnName(Wv2State, 'shared-pre-existing') + '","evidence":{"registry":"HKLM\\SOFTWARE\\WOW6432Node\\Microsoft\\EdgeUpdate\\Clients"}},' +
    '"postgresql":{"ownership":"' + OwnName(PgState, 'pre-existing') + '","evidence":{"service_scan":"sc query, exit code checked"}}}}';
  try
    ForceDirectories(StateDir());
    RecordOk := SaveStringToFile(StateDir() + '\ownership-state.json', OJson + #13#10, False) and RecordOk;
  except
    RecordOk := False;
  end;
  if not RecordOk then
  begin
    JEvent('rollback-left-behind', ',"kind":"ownership-record","reason":"ownership files could not be written; nothing was removed"');
    RaiseException('Install finished but the ownership record could not be written. Nothing was removed. Check ' + StateDir());
  end;
  if not JEvent('install-complete', '') then
    RaiseException('Install finished but the journal could not record completion. Nothing was removed.');
end;

function InitializeUninstall(): Boolean;
var
  ExitCode: Integer;
begin
  Result := False;
  if not Exec(ExpandConstant('{cmd}'), '/C sc.exe query HOnYBackend', '', SW_HIDE, ewWaitUntilTerminated, ExitCode) then
  begin
    MsgBox('The HOnYBackend service state could not be checked (sc.exe did not run). Application files were preserved.', mbError, MB_OK);
    Exit;
  end;
  if ExitCode = 0 then
    MsgBox('Remove the HOnYBackend service with the reviewed removal script before uninstalling application files. Data and certificates must be preserved.', mbError, MB_OK)
  else if ExitCode = 1060 then
    Result := True
  else
    MsgBox('Service state could not be verified (sc exit code ' + IntToStr(ExitCode) + '). Application files were preserved.', mbError, MB_OK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep <> usPostUninstall then Exit;
  { No automatic cleanup beyond Inno's own removal of the files it installed. There is
    no [UninstallDelete] section and no scripted deletion: the derived .venv, data,
    .env, ProgramData, certificates and keys all stay in place for manual handling.
    Runtimes follow the recorded ownership state, report-only: }
  if FileExists(StateDir() + '\python.owned-by-hony') then
    MsgBox('This HOnY installation installed Python 3.11.9 itself. It was left in place: ' +
      'remove "Python 3.11.9 (64-bit)" through Windows Apps if you no longer need it. ' +
      'The ownership record in ' + StateDir() + ' is kept for audit.', mbInformation, MB_OK)
  else if FileExists(ExpandConstant('{autopf}\HOnY\python\python.exe')) then
    MsgBox('A pre-existing Python runtime was found and left untouched.', mbInformation, MB_OK);
  { WebView2 is a machine-wide shared runtime: never removed by this uninstaller. }
end;
