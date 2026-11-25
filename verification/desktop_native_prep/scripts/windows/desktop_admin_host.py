"""Isolated admin command entry, used with Python -I from reviewed scripts.

Only the fixed administrator-controlled application root is added to sys.path.
No arbitrary module/path/code argument is accepted. This is not an elevation
mechanism and does not itself grant deployment authority.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    if len(sys.argv) < 2:
        raise SystemExit("An explicit supported operation is required.")
    operation = sys.argv[1]
    if operation == "certificate":
        from app.desktop_certificate import main as certificate_main
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        certificate_main()
    elif operation == "service":
        import win32serviceutil
        from app.desktop_service import HOnYService
        # Exact fixed invocation shapes only, no arbitrary pywin32 options.
        install = ["--startup", "delayed", "--username", r"NT AUTHORITY\NetworkService", "install"]
        arguments = sys.argv[2:]
        if arguments not in (["remove"], install):
            raise SystemExit("Only the reviewed NetworkService install or service removal is supported.")
        win32serviceutil.HandleCommandLine(HOnYService, argv=[sys.argv[0], *arguments])
    else:
        raise SystemExit("Unsupported operation.")


if __name__ == "__main__":
    main()
