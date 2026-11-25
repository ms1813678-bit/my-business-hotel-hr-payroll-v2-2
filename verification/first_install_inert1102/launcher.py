"""UNEXECUTED fixed Windows CI launcher. No CLI arguments permitted.
Native execution remains held pending main source/action review.
"""
import json
import os
from pathlib import Path
import sys
from native import Native
from slice import Slice, Refused
import restricted_checks

IDENTIFIER = "64e7dd159c6a4df1a57f690f2cfd1045"

def main():
    if sys.platform != "win32" or os.environ.get("GITHUB_ACTIONS") != "true" or len(sys.argv) != 1:
        raise Refused("fixed disposable Windows CI launcher only")
    native = None
    flow = None
    result = {"identifier": IDENTIFIER, "status": "not-started", "ready": False,
              "service_start_exercised": False, "actual_service_token_tested": False}
    failures = []
    try:
        native = Native(IDENTIFIER)
        flow = Slice(native)
        result["status"] = flow.run()
        result["restricted_access"] = restricted_checks.check(native)
        # Exact native readback occurred before reporting staged-not-ready.
        result["status"] = "staged-not-ready-checked"
    except BaseException as exc:
        failures.append({"phase": "attempt", "error": type(exc).__name__, "detail": str(exc)})
    finally:
        if native is not None and not native.elevated_context_restored:
            failures.append({"phase": "security-context", "error": "cleanup-refused-context-unrestored"})
        if flow is not None and native.elevated_context_restored:
            try:
                cleanup = flow.rollback()
                failures.extend({"phase": "rollback", "error": value} for value in cleanup)
                result["rollback_complete"] = flow.done
            except BaseException as exc:
                failures.append({"phase": "rollback", "error": type(exc).__name__, "detail": str(exc)})
        if native is not None:
            try:
                native.close()
            except BaseException as exc:
                failures.append({"phase": "close", "error": type(exc).__name__, "detail": str(exc)})
        if native is not None and flow is not None and flow.done:
            try:
                result["final_absence"] = native.final_absence()
            except BaseException as exc:
                failures.append({"phase": "final-absence", "error": type(exc).__name__, "detail": str(exc)})
        result["failures"] = failures
        # Non-secret structured stdout only, no arbitrary output filesystem path.
        print(json.dumps(result, sort_keys=True), flush=True)
    return 1 if failures or not result.get("rollback_complete") else 0

if __name__ == "__main__":
    raise SystemExit(main())
