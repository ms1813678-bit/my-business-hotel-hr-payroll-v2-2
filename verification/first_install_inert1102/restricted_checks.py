"""UNEXECUTED negative access-open probes, no file/service mutations.
Actual service process token and runtime directory positives need later start.
"""
from slice import Refused

def check(native):
    s, c, f, ws = native.sec, native.c, native.f, native.ws
    source = restricted = impersonation = None
    impersonating = False
    results = []
    try:
        source = s.OpenProcessToken(native.api.GetCurrentProcess(), s.TOKEN_DUPLICATE | s.TOKEN_QUERY)
        admin = s.CreateWellKnownSid(s.WinBuiltinAdministratorsSid, None)
        system = s.CreateWellKnownSid(s.WinLocalSystemSid, None)
        restricted = s.CreateRestrictedToken(source, s.DISABLE_MAX_PRIVILEGE,
                                              [(admin, 0), (system, 0)], None, None)
        impersonation = s.DuplicateTokenEx(restricted, s.SecurityImpersonation,
                                           s.TOKEN_QUERY | s.TOKEN_IMPERSONATE,
                                           s.TokenImpersonation, None)
        # Verify reduction before impersonating or attempting access.
        if s.CheckTokenMembership(impersonation, admin):
            raise Refused("restricted token still has enabled Administrators")
        native.elevated_context_restored = False
        try:
            s.ImpersonateLoggedOnUser(impersonation)
        except BaseException:
            # Failed call has not established impersonation; prove original context.
            native.elevated_context_restored = bool(s.CheckTokenMembership(None, admin))
            raise
        impersonating = True
        if s.CheckTokenMembership(None, admin):
            raise Refused("thread reduction not effective")
        # Positive read control on already-held trusted Program Files parent.
        # No new file, ACL grant or product resource. A refusal fails qualification.
        h = f.CreateFile(str(native.parent), c.GENERIC_READ | c.READ_CONTROL,
                         c.FILE_SHARE_READ | c.FILE_SHARE_WRITE | c.FILE_SHARE_DELETE,
                         None, c.OPEN_EXISTING,
                         f.FILE_FLAG_OPEN_REPARSE_POINT | c.FILE_FLAG_BACKUP_SEMANTICS, None)
        try:
            native.identity(h, native.parent, True)
        finally:
            h.Close()
        results.append("trusted-parent-read:granted")
        for name, path, directory, rights in (
            ("root-create-child", native.root, True, 0x2 | 0x4),
            ("root-delete", native.root, True, c.DELETE),
            ("root-dacl", native.root, True, c.WRITE_DAC),
            ("payload-write", native.root / "payload.txt", False, c.GENERIC_WRITE),
            ("payload-delete", native.root / "payload.txt", False, c.DELETE),
            ("payload-dacl", native.root / "payload.txt", False, c.WRITE_DAC),
        ):
            try:
                h = f.CreateFile(str(path), rights,
                                  c.FILE_SHARE_READ | c.FILE_SHARE_WRITE | c.FILE_SHARE_DELETE,
                                  None, c.OPEN_EXISTING,
                                  f.FILE_FLAG_OPEN_REPARSE_POINT | (c.FILE_FLAG_BACKUP_SEMANTICS if directory else 0), None)
            except native.types.error as exc:
                # Sharing/path failures are NOT an ACL-denial pass.
                if exc.winerror != 5:
                    raise Refused(name + " not proven access denied: " + str(exc.winerror))
                results.append(name + ":access-denied")
            else:
                h.Close()
                raise Refused(name + " unexpectedly granted")
        scm = ws.OpenSCManager(None, None, ws.SC_MANAGER_CONNECT)
        try:
            for name, rights in (("service-config", ws.SERVICE_CHANGE_CONFIG),
                                 ("service-start", ws.SERVICE_START),
                                 ("service-delete", c.DELETE)):
                try:
                    h = ws.OpenService(scm, native.name, rights)
                except native.types.error as exc:
                    if exc.winerror != 5:
                        raise Refused(name + " not proven access denied: " + str(exc.winerror))
                    results.append(name + ":access-denied")
                else:
                    ws.CloseServiceHandle(h)
                    raise Refused(name + " unexpectedly granted")
        finally:
            ws.CloseServiceHandle(scm)
    finally:
        if impersonating:
            # If this fails, do not attempt elevated cleanup from this thread.
            s.RevertToSelf()
            if not s.CheckTokenMembership(None, s.CreateWellKnownSid(s.WinBuiltinAdministratorsSid, None)):
                raise Refused("elevated context not restored")
            native.elevated_context_restored = True
        if impersonation is not None:
            impersonation.Close()
        if restricted is not None:
            restricted.Close()
        if source is not None:
            source.Close()
    return results
