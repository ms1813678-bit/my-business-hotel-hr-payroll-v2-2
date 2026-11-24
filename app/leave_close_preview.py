"""Close-operation prerequisites, not a permission decision or close mutation."""
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class BackupEvidence:
    run_id: int
    status: str
    verified_at: datetime | None
    archive_path: str | None
    sha256: str | None


def validate_close_prerequisites(backup: BackupEvidence, policy_snapshot: dict[str, Any]) -> str:
    """Validate supplied fresh backup evidence and serialize explicit policy.

    A host BackupRun row is necessary but not sufficient: the caller still must
    freshly verify the archive, establish operator authority, serialize register
    writes, audit and commit the month-close fact. This function does none of
    those actions, never creates a backup and never authorizes closure itself.
    """
    if isinstance(backup.run_id, bool) or not isinstance(backup.run_id, int) or backup.run_id < 1:
        raise ValueError("Verified host backup run is required.")
    if backup.status != 'success' or backup.verified_at is None or not backup.archive_path:
        raise ValueError("Host backup must have successful verification evidence.")
    if backup.verified_at.tzinfo is None or backup.verified_at.utcoffset() is None:
        raise ValueError("Backup verification timestamp must be timezone-aware.")
    if (not isinstance(backup.sha256, str) or len(backup.sha256) != 64
            or any(character not in '0123456789abcdef' for character in backup.sha256)):
        raise ValueError("Host backup digest is missing or invalid.")
    if not isinstance(policy_snapshot, dict) or not policy_snapshot:
        raise ValueError("Explicit nonempty policy snapshot required, no inferred defaults.")
    return json.dumps(policy_snapshot, ensure_ascii=False, sort_keys=True, allow_nan=False)
