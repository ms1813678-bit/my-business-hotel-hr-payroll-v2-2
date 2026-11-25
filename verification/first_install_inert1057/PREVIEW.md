# Exact stopped-only synthetic preview, 25 November 2026 10:57 Cairo

SOURCE REVIEW ONLY. Native execution is NOT approved. No product wiring or workflow added. Native.py has not been imported on Windows or executed. Twenty-eight pure lifecycle/native-adapter simulation tests and Python compilation pass; neither is real Windows evidence.

## Fixed proposed later run

- Runner: fresh disposable Windows-2025 CI with an elevated administrator. Not hotel PC.
- Identifier: 64e7dd159c6a4df1a57f690f2cfd1045, fixed for this preview. Collision refuses, never silently chooses another path.
- Parent: shell-resolved Program Files path must resolve to the system-drive Program Files directory and pass held ancestry/owner/DACL checks. For the expected runner this is C:\Program Files. Environment-derived name alone is not authority. Source now resolves the shell Program Files folder, compares the Windows system drive, requires fixed local NTFS/persistent ACLs and validates real held ancestry/security. These APIs are unexecuted.
- Only created directory: C:\Program Files\HOnYInertFixture_64e7dd159c6a4df1a57f690f2cfd1045.
- Only created files: payload.txt (fixed synthetic text), manifest.json (synthetic=true, ready=false), journal.jsonl (non-secret event records).
- Service: HOnYInertFixture64e7dd159c6a4df1a57f690f2cfd1045, own-process, DISABLED, STOPPED, NetworkService account, no dependencies, no failure restart actions configured. Exact binary command is the quoted absolute path C:\Program Files\HOnYInertFixture_64e7dd159c6a4df1a57f690f2cfd1045\NOT_EXECUTABLE_SYNTHETIC_ONLY.exe. That file does not exist and is never created or run. No service-start or stop API in this slice.
- Root creation SDDL: owner/group Administrators, protected DACL, SYSTEM and Administrators full access with OI/CI inheritance. Files use a separate explicit protected descriptor with the same owner/audience/full rights and no inheritance flags. Exact readback distinguishes directory flags3 from file flags0, never accepts either interchangeably. No Users/Everyone/NetworkService access grant. Service DACL allows SYSTEM/Administrators only.
- No production HOnYBackend, ProgramData, existing release, shortcut, PostgreSQL, TLS/certificate/key, hosts, firewall, trust store, scheduled task, registry outside this exact service key, network bind or external account change.

## Exact sequence

1. Read elevated membership, trusted path resolution and existing ancestor object identities/security; hold ancestors while work proceeds. Refuse unsupported/object-specific ACEs, reparse paths, unsafe owner/mutation audience. Current conservative ACL implementation is unqualified and may refuse normal configurations; refusal is not product incompatibility.
2. Open SCM with connect/create rights and check exact service absence. No change on collision. Exclusive directory/file/service create remains the authority for created ownership, not precheck.
3. Create exact root with restrictive creation-time descriptor. Hold read/delete handle, validate volume/file identity, resolved path, owner and protected ACL. A failure before retained handle is Unknown and preserves rather than deleting by name. This is an ordinary-user protected-parent design, not atomic hostile-admin proof.
4. Exclusively create and hold journal, append/flush events, create and hash/read back the two bounded payload files. Journal is updated with intent and observed owned result. Crash durability and automatic restart recovery are NOT qualified.
5. Create exact service disabled, set/read back its DACL, query full configuration and stopped state/PID0. Return staged-not-ready, never ready. No service-specific SID or actual service-token test claimed by this inert slice.
6. Always rollback during disposable qualification. Delete only retained exact disabled/stopped service handle after config/state recheck, close once, query exact service absence with ten-second bound. Unknown service/effect pins files/root.
7. Remove each owned payload file by retained handle disposition, close once and confirm absence. Independent file cleanup continues after another file failure, but root is pinned. Journal then root are removed only if prerequisites permit. No recursive deletion or path reopen fallback. Close remaining handles independently and report every fault.
8. Verify exact service/root absence against original baseline. Collision resources must remain unchanged. Runner disposal is a fallback boundary, not proof cleanup succeeded.

## Scope of checks

Pure tests currently establish only state-machine behavior: successful staged-not-ready + rollback; Owned verification failures retain cleanup authority; Unknown/forged/malformed results preserve; service failure pins payload; independent file cleanup; one-shot/terminal behavior; grammar; absence of start/stop/product strings in source.

Native inert checks still required after source review and execution approval: real API signatures, supported handle access/sharing, real descriptor owner/DACL/volume/path identity, collision preservation, actual journal failure injection, service security/config exact readback, absence proof and cleanup under Windows.

Restricted-token negative checks are now implemented in restricted_checks.py: create restricted token with admin/system groups disabled and privileges removed, duplicate impersonation token using the unchanged documented five-position signature, verify disabled membership, impersonate, verify a positive read handle on the already-trusted existing Program Files parent, then request root create-child/delete/DACL and payload write/delete/DACL handles, then service configuration/start/delete handles. Every probe must get ERROR_ACCESS_DENIED specifically; sharing/path failures do not count. It never creates/mutates a tamper object. Always revert, verify administrative context restored, close tokens. If reversion cannot be proved, launcher refuses mutation cleanup and preserves diagnostics. These are implemented but UNEXECUTED. They approximate an ordinary-user rights boundary, not a real named standard-user installation test or actual backend token.

Actual service-specific process-token/runtime effective access and service health require a later controlled-start slice. A stopped disabled service has no runtime process token to inspect. No service SID/runtime positive check is claimed here. File handles allow read/write/delete sharing solely so negative probe access is decided by ACL rather than guaranteed sharing rejection; protected parent/DACL is the ordinary-user boundary, not a share-lock claim.

## Corrected source and next review gate

- Environment-path trust replaced by system shell Program Files resolution, Windows system-drive binding and fixed NTFS/persistent ACL checks.
- Service DACL protection/rights/principal equality checked, disabled/stopped/PID0 and exact config checked. Files/root verify full/protected exact audience.
- Journal failure pins uncertain ownership. Adapter simulation cases cover owned journal failure, unknown journal failure, root create/open/verify distinctions, instance-bound tokens, close uncertainty and launcher finally propagation.
- Launcher is fixed to this identifier and Windows CI, no arbitrary path/options. It always rollback/close, nonzero on any failure, preserves on unrecovered security context, and independently checks final root/service absence after success. It does not grant its own execution approval.
- Full source review is still required for PyWin32 constants/API behavior, handle/security/inheritance assumptions, restricted-token semantics and all absent native evidence. These may reveal additional defects. No execution permission requested by a test pass.
- No power-loss durability/automatic-recovery guarantee, product launcher/payload/runtime/TLS/DB/bootstrap or final hotel acceptance. State is staged-not-ready only.


## References

https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createdirectoryw
https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-setfileinformationbyhandle
https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers
https://learn.microsoft.com/en-us/windows/win32/api/winsvc/nf-winsvc-createservicew
https://mhammond.github.io/pywin32/win32file__CreateDirectory_meth.html
https://mhammond.github.io/pywin32/win32file__SetFileInformationByHandle_meth.html
https://mhammond.github.io/pywin32/win32security__SetSecurityInfo_meth.html
https://mhammond.github.io/pywin32/win32service__SetServiceObjectSecurity_meth.html

## 10:57 focused review follow-up

No DuplicateTokenEx edit: exact1053 archive already matched its documented signature. New call-shape simulation confirms argument order. Failed ImpersonateLoggedOnUser now checks original administrative membership before marking context restored; if the check fails, cleanup still stays held. Successful impersonation always needs RevertToSelf plus membership readback.

Added restricted read-positive on existing trusted parent, no new ACL audience or tamper write. A denied positive read fails the experiment instead of counting blanket failures as negative-access proof. This remains a restricted-token approximation, not an actual standard-user or backend process token.

Files now deliberately receive a non-inheritable explicit full-access SYSTEM/Administrators descriptor; directories retain OI/CI. This avoids speculating whether file creation strips inheritance bits. Exact owner, protection, audience, effective rights and flags remain checked. Service readback additionally checks normal error control, empty group, zero tag, empty dependency list and exact display name. No cleanup wait/forced cleanup changes.

Seven new simulation tests cover duplicate-token call shape, positive read plus nine denials, failed impersonation tracking, failed positive read, sharing errors, file/directory descriptor flags, and service auxiliary configuration rejection. Total28 tests pass without importing Windows APIs. All native behavior remains unexecuted and hold remains.

https://mhammond.github.io/pywin32/win32security__DuplicateTokenEx_meth.html
https://learn.microsoft.com/en-us/windows/win32/secauthz/ace-inheritance-rules
https://mhammond.github.io/pywin32/win32service__QueryServiceConfig_meth.html
