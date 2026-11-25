"""UNEXECUTED Windows adapter source for fixed inert synthetic slice.
No CLI, no service-start function. Import is non-mutating. Fixed launcher provided separately.
"""
import hashlib
import json
import os
from pathlib import PureWindowsPath
import subprocess
import time
from slice import Kind, Result, Refused, SDDL, fixture_id

class Native:
    def __init__(self, identifier):
        if os.name != "nt":
            raise Refused("Windows only")
        import win32api, win32con, win32file, win32security, win32service, pywintypes
        self.api, self.c, self.f, self.sec, self.ws, self.types = win32api, win32con, win32file, win32security, win32service, pywintypes
        self.id = fixture_id(identifier)
        # No user-supplied parent, no product destination.
        from win32com.shell import shell, shellcon
        self.parent = PureWindowsPath(shell.SHGetFolderPath(0, shellcon.CSIDL_PROGRAM_FILES, 0, 0))
        self.system_drive = PureWindowsPath(self.api.GetWindowsDirectory()).drive
        self.root = self.parent / ("HOnYInertFixture_" + self.id)
        self.name = "HOnYInertFixture" + self.id
        self.command = subprocess.list2cmdline([str(self.root / "NOT_EXECUTABLE_SYNTHETIC_ONLY.exe")])
        self.records = {}
        self.ancestors = []
        self.scm = None
        self.root_pinned = False
        self.journal_token = None
        self.elevated_context_restored = True

    def descriptor(self):
        return self.sec.ConvertStringSecurityDescriptorToSecurityDescriptor(SDDL, self.sec.SDDL_REVISION_1)

    def attributes(self, directory=True):
        sa = self.types.SECURITY_ATTRIBUTES()
        sa.SECURITY_DESCRIPTOR = (self.descriptor() if directory else self.sec.ConvertStringSecurityDescriptorToSecurityDescriptor(
            "O:BAG:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)", self.sec.SDDL_REVISION_1))
        sa.bInheritHandle = False
        return sa

    def identity(self, h, path, directory):
        info = self.f.GetFileInformationByHandle(h)
        attrs = info[0]
        if attrs & self.c.FILE_ATTRIBUTE_REPARSE_POINT:
            raise Refused("reparse object")
        if bool(attrs & self.c.FILE_ATTRIBUTE_DIRECTORY) != directory:
            raise Refused("object kind mismatch")
        if not directory and info[7] != 1:
            raise Refused("non-exclusive link count")
        resolved = self.f.GetFinalPathNameByHandle(h, 0)
        if resolved.casefold() != ("\\\\?\\" + str(path)).casefold():
            raise Refused("resolved path mismatch")
        return (info[4], info[8], info[9])

    def hold(self, path, directory, write=False):
        access = self.c.GENERIC_READ | self.c.READ_CONTROL
        if write:
            access |= self.c.DELETE
            if not directory:
                access |= self.c.GENERIC_WRITE
        return self.f.CreateFile(str(path), access, self.c.FILE_SHARE_READ, None,
                                 self.c.OPEN_EXISTING,
                                 self.c.FILE_FLAG_BACKUP_SEMANTICS | self.c.FILE_FLAG_OPEN_REPARSE_POINT, None)

    def acl(self, h, exact=False, final_parent=True, directory=True):
        sd = self.sec.GetSecurityInfo(h, self.sec.SE_FILE_OBJECT,
                                     self.sec.OWNER_SECURITY_INFORMATION | self.sec.DACL_SECURITY_INFORMATION)
        owner = self.sec.ConvertSidToStringSid(sd.GetSecurityDescriptorOwner())
        allowed_owners = {"S-1-5-18", "S-1-5-32-544", "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"}
        if owner not in allowed_owners:
            raise Refused("untrusted ancestor owner")
        acl = sd.GetSecurityDescriptorDacl()
        if acl is None:
            raise Refused("null DACL")
        allowed = {"S-1-5-18", "S-1-5-32-544"}
        # Mutating rights relevant to replacing children/code or rewriting DACL.
        mutate = 0x40000000 | 0x10000000 | 0x00010000 | 0x00040000 | 0x00080000 | 0x40
        if final_parent:
            mutate |= 0x2 | 0x4 | 0x10 | 0x100
        seen = set()
        for index in range(acl.GetAceCount()):
            ace = acl.GetAce(index)
            if ace[0][0] != self.sec.ACCESS_ALLOWED_ACE_TYPE:
                # Unsupported/object-specific ACEs need explicit review, not omission.
                raise Refused("unsupported ancestor ACE")
            sid = self.sec.ConvertSidToStringSid(ace[2])
            if exact:
                if sid not in allowed or sid in seen or ace[1] not in (0x1F01FF, self.c.GENERIC_ALL) or ace[0][1] != (3 if directory else 0):
                    raise Refused("fixture ACL rights/audience mismatch")
                seen.add(sid)
            # INHERIT_ONLY does not grant access on this object.
            if not ace[0][1] & self.c.INHERIT_ONLY_ACE and ace[1] & mutate and sid not in allowed_owners:
                raise Refused("ancestor writable outside trusted administrators")
        if exact:
            if seen != allowed:
                raise Refused("fixture ACL missing trusted principal")
            control, _ = sd.GetSecurityDescriptorControl()
            if not control & self.sec.SE_DACL_PROTECTED:
                raise Refused("fixture DACL inheritance not protected")
            if owner != "S-1-5-32-544":
                raise Refused("fixture owner mismatch")

    def preflight(self):
        token = self.sec.OpenProcessToken(self.api.GetCurrentProcess(), self.sec.TOKEN_QUERY)
        try:
            if not self.sec.GetTokenInformation(token, self.sec.TokenElevation):
                raise Refused("elevated token required")
        finally:
            token.Close()
        if not self.sec.CheckTokenMembership(None, self.sec.CreateWellKnownSid(self.sec.WinBuiltinAdministratorsSid, None)):
            raise Refused("elevated administrator required")
        if self.parent.drive.upper() != self.system_drive.upper():
            raise Refused("system volume ProgramFiles required")
        volume = self.api.GetVolumeInformation(str(self.parent.anchor))
        if volume[4].upper() != "NTFS" or not volume[3] & 0x8:
            raise Refused("local NTFS persistent ACL volume required")
        if self.f.GetDriveType(str(self.parent.anchor)) != self.c.DRIVE_FIXED:
            raise Refused("local fixed drive required")
        path = PureWindowsPath(self.parent.anchor)
        try:
            for component in (None, *self.parent.parts[1:]):
                if component is not None:
                    path /= component
                h = self.hold(path, True)
                self.ancestors.append((h, path, self.identity(h, path, True)))
                self.acl(h, final_parent=path == self.parent)
            self.scm = self.ws.OpenSCManager(None, None, self.ws.SC_MANAGER_CONNECT | self.ws.SC_MANAGER_CREATE_SERVICE)
            # Read-only collision check is courtesy; CreateService is exclusive authority.
            try:
                h = self.ws.OpenService(self.scm, self.name, self.ws.SERVICE_QUERY_STATUS)
            except self.types.error as exc:
                if exc.winerror != 1060:
                    raise
            else:
                self.ws.CloseServiceHandle(h)
                raise Refused("foreign service collision")
        except BaseException:
            self.close()
            raise

    def validate_parent(self):
        for h, path, identity in self.ancestors:
            if self.identity(h, path, True) != identity:
                raise Refused("held ancestor changed")
            self.acl(h, final_parent=path == self.parent)

    def register(self, operation, h, path=None, directory=False):
        token = object()
        self.records[token] = {"operation": operation, "handle": h, "path": path,
                               "directory": directory, "identity": None, "closed": False, "absent": False}
        return token

    def issued(self, token, operation):
        return token in self.records and self.records[token]["operation"] == operation

    def create_root(self):
        self.validate_parent()
        try:
            self.f.CreateDirectory(str(self.root), self.attributes())
        except self.types.error as exc:
            if exc.winerror == 183:
                return Result(Kind.NO_EFFECT, detail="foreign root collision")
            return Result(Kind.UNKNOWN, detail="directory create unresolved " + str(exc.winerror))
        # Accepted ordinary-user boundary: parent denies untrusted namespace mutation.
        # Failure between create and held readback is Unknown, not path cleanup.
        try:
            h = self.hold(self.root, True, write=True)
        except BaseException:
            return Result(Kind.UNKNOWN, detail="created root has no retained handle")
        token = self.register("root", h, self.root, True)
        try:
            self.records[token]["identity"] = self.identity(h, self.root, True)
            self.acl(h, exact=True)
        except BaseException as exc:
            return Result(Kind.OWNED, token, "root verification failed " + type(exc).__name__)
        return Result(Kind.OWNED, token)

    def record(self, token):
        if token not in self.records:
            raise Refused("foreign token")
        r = self.records[token]
        if r["closed"] or r["absent"]:
            raise Refused("consumed token")
        if r["operation"] != "service":
            if r["identity"] is None or self.identity(r["handle"], r["path"], r["directory"]) != r["identity"]:
                raise Refused("retained object identity unresolved")
            self.acl(r["handle"], exact=True, directory=r["directory"])
        return r

    def create_file(self, root, name, data):
        self.record(root)
        if name not in {"payload.txt", "manifest.json", "journal.jsonl"}:
            raise Refused("fixed payload only")
        path = self.root / name
        try:
            h = self.f.CreateFile(str(path), self.c.GENERIC_READ | self.c.GENERIC_WRITE | self.c.DELETE | self.c.READ_CONTROL,
                                  self.c.FILE_SHARE_READ | self.c.FILE_SHARE_WRITE | self.c.FILE_SHARE_DELETE, self.attributes(directory=False), self.c.CREATE_NEW, self.c.FILE_FLAG_OPEN_REPARSE_POINT, None)
        except self.types.error as exc:
            if exc.winerror in (80, 183):
                return Result(Kind.NO_EFFECT, detail="foreign file collision")
            return Result(Kind.UNKNOWN, detail="file create unresolved " + str(exc.winerror))
        token = self.register(name, h, path)
        try:
            self.records[token]["identity"] = self.identity(h, path, False)
            self.acl(h, exact=True, directory=False)
            written = self.f.WriteFile(h, data)
            if written[0] != 0 or written[1] != len(data):
                raise Refused("incomplete file write")
            self.f.FlushFileBuffers(h)
            self.verify_file(token, hashlib.sha256(data).hexdigest())
        except BaseException as exc:
            return Result(Kind.OWNED, token, "file stage failed " + type(exc).__name__)
        return Result(Kind.OWNED, token)

    def verify_file(self, token, expected):
        r = self.record(token)
        self.f.SetFilePointer(r["handle"], 0, self.c.FILE_BEGIN)
        size = self.f.GetFileSize(r["handle"])
        if size > 65536:
            raise Refused("bounded fixture file required")
        raw = self.f.ReadFile(r["handle"], size)[1]
        if hashlib.sha256(raw).hexdigest() != expected:
            raise Refused("payload readback mismatch")

    def journal(self, root, events):
        if self.journal_token is None:
            result = self.create_file(root, "journal.jsonl", b"")
            if result.kind is Kind.OWNED:
                self.journal_token = result.token
            if result.kind is not Kind.OWNED or result.detail:
                self.pin_root()
                raise Refused("journal create unresolved")
        r = self.record(self.journal_token)
        self.f.SetFilePointer(r["handle"], 0, self.c.FILE_END)
        data = (json.dumps(events, separators=(",", ":")) + "\n").encode()
        written = self.f.WriteFile(r["handle"], data)
        if written[0] != 0 or written[1] != len(data):
            raise Refused("incomplete journal write")
        self.f.FlushFileBuffers(r["handle"])

    def create_service(self, root):
        self.record(root)
        access = self.ws.SERVICE_QUERY_CONFIG | self.ws.SERVICE_QUERY_STATUS | self.c.DELETE | self.c.READ_CONTROL | self.c.WRITE_DAC
        try:
            h = self.ws.CreateService(self.scm, self.name, self.name, access,
                                       self.ws.SERVICE_WIN32_OWN_PROCESS, self.ws.SERVICE_DISABLED,
                                       self.ws.SERVICE_ERROR_NORMAL, self.command, None, 0, None,
                                       "NT AUTHORITY\\NetworkService", None)
        except self.types.error as exc:
            if exc.winerror in (1073, 1078):
                return Result(Kind.NO_EFFECT, detail="foreign service collision")
            return Result(Kind.UNKNOWN, detail="service create unresolved " + str(exc.winerror))
        token = self.register("service", h)
        try:
            service_sd = self.sec.ConvertStringSecurityDescriptorToSecurityDescriptor(
                "D:P(A;;GA;;;SY)(A;;GA;;;BA)", self.sec.SDDL_REVISION_1)
            self.ws.SetServiceObjectSecurity(h, self.sec.DACL_SECURITY_INFORMATION | self.sec.PROTECTED_DACL_SECURITY_INFORMATION, service_sd)
            self.verify_service(token)
        except BaseException as exc:
            return Result(Kind.OWNED, token, "stopped service verification failed " + type(exc).__name__)
        return Result(Kind.OWNED, token)

    def verify_service(self, token):
        r = self.record(token)
        c = self.ws.QueryServiceConfig(r["handle"])
        state = self.ws.QueryServiceStatusEx(r["handle"])
        sd = self.ws.QueryServiceObjectSecurity(r["handle"], self.sec.DACL_SECURITY_INFORMATION)
        control, _ = sd.GetSecurityDescriptorControl()
        if not control & self.sec.SE_DACL_PROTECTED:
            raise Refused("service DACL not protected")
        acl = sd.GetSecurityDescriptorDacl()
        if acl is None or acl.GetAceCount() != 2:
            raise Refused("service DACL shape mismatch")
        seen = set()
        for index in range(acl.GetAceCount()):
            ace = acl.GetAce(index)
            sid = self.sec.ConvertSidToStringSid(ace[2])
            if ace[0] != (self.sec.ACCESS_ALLOWED_ACE_TYPE, 0) or sid not in {"S-1-5-18", "S-1-5-32-544"} or sid in seen or ace[1] not in (self.ws.SERVICE_ALL_ACCESS, self.c.GENERIC_ALL):
                raise Refused("service DACL rights/audience mismatch")
            seen.add(sid)
        if c[2] != self.ws.SERVICE_ERROR_NORMAL or c[4] not in (None, "") or c[5] != 0 or c[6] not in (None, [], ()) or c[8] != self.name:
            raise Refused("exact service auxiliary config mismatch")
        if c[0] != self.ws.SERVICE_WIN32_OWN_PROCESS or c[1] != self.ws.SERVICE_DISABLED or c[3] != self.command or c[7].lower() != "nt authority\\networkservice" or state["CurrentState"] != self.ws.SERVICE_STOPPED or state["ProcessId"]:
            raise Refused("exact disabled/stopped service mismatch")

    def pin_root(self):
        self.root_pinned = True

    def remove(self, token):
        r = self.records.get(token)
        if r is None:
            raise Refused("foreign cleanup token")
        if r["absent"]:
            return
        r = self.record(token)
        if r["operation"] == "service":
            self.verify_service(token)
            self.ws.DeleteService(r["handle"])
            r["closed"] = True
            self.ws.CloseServiceHandle(r["handle"])
            deadline = time.monotonic() + 10
            while True:
                try:
                    h = self.ws.OpenService(self.scm, self.name, self.ws.SERVICE_QUERY_STATUS)
                except self.types.error as exc:
                    if exc.winerror == 1060:
                        break
                    if time.monotonic() >= deadline:
                        raise
                else:
                    self.ws.CloseServiceHandle(h)
                    if time.monotonic() >= deadline:
                        raise Refused("service absence unconfirmed")
                time.sleep(.1)
        else:
            if r["operation"] == "root":
                if self.root_pinned:
                    raise Refused("root pinned after child failure")
                if self.journal_token is not None:
                    self.remove(self.journal_token)
            self.f.SetFileInformationByHandle(r["handle"], self.f.FileDispositionInfo, True)
            r["closed"] = True
            r["handle"].Close()
            try:
                attrs = self.f.GetFileAttributes(str(r["path"]))
            except self.types.error as exc:
                if exc.winerror not in (2, 3):
                    raise
            else:
                raise Refused("object absence unconfirmed")
        r["absent"] = True

    def final_absence(self):
        try:
            self.f.GetFileAttributes(str(self.root))
        except self.types.error as exc:
            if exc.winerror not in (2, 3):
                raise
        else:
            raise Refused("fixture root still present")
        scm = self.ws.OpenSCManager(None, None, self.ws.SC_MANAGER_CONNECT)
        try:
            try:
                h = self.ws.OpenService(scm, self.name, self.ws.SERVICE_QUERY_STATUS)
            except self.types.error as exc:
                if exc.winerror != 1060:
                    raise
            else:
                self.ws.CloseServiceHandle(h)
                raise Refused("fixture service still present")
        finally:
            self.ws.CloseServiceHandle(scm)
        return True

    def close(self):
        errors = []
        for r in self.records.values():
            if not r["closed"]:
                r["closed"] = True
                try:
                    if r["operation"] == "service":
                        self.ws.CloseServiceHandle(r["handle"])
                    else:
                        r["handle"].Close()
                except BaseException as exc:
                    errors.append(type(exc).__name__)
        for h, _, _ in reversed(self.ancestors):
            try:
                h.Close()
            except BaseException as exc:
                errors.append(type(exc).__name__)
        self.ancestors.clear()
        if self.scm is not None:
            h, self.scm = self.scm, None
            try:
                self.ws.CloseServiceHandle(h)
            except BaseException as exc:
                errors.append(type(exc).__name__)
        if errors:
            raise Refused("close errors: " + ",".join(errors))
