"""Secret-free failure structure for local service events.

Never includes exception messages, locals, source text, absolute paths, request
URLs or database values. Module basenames/functions/line numbers retain useful
location evidence without forwarding a raw traceback that may contain secrets.
"""
import json
import traceback
from pathlib import Path


def failure_structure(error: BaseException) -> str:
    frames = traceback.extract_tb(error.__traceback__)
    return json.dumps({
        "error_type": type(error).__name__,
        "frames": [{"module": Path(frame.filename).name,
                    "function": frame.name, "line": frame.lineno} for frame in frames[-12:]],
    }, ensure_ascii=True)
