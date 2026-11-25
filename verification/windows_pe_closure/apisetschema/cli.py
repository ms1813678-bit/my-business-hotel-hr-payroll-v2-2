"""Read-only command-line driver: parse the .apiset section of an
apisetschema.dll image as a v6 namespace and print the mapping document,
evidence and a provenance report as JSON on stdout.

Usage:
    python -m apisetschema.cli PATH [--os-build LABEL] [--source-label LABEL]

Read-only guarantees: the image is opened O_RDONLY (+O_BINARY where the
platform defines it), the size cap is enforced from fstat before content is
read, nothing is written, no DLL is loaded or executed, and no network access
is performed. Standard library only.

Exit codes: 0 parsed; 2 format error; 3 unsupported namespace version (every
version other than 6, including 2 and 4); 4 usage error.
"""
from __future__ import annotations

import hashlib
import json
import sys

from .mapping import build_mapping, full_report
from .namespace import (BoundsExceeded, NamespaceFormatError,
                        UnsupportedVersionError, parse_namespace)
from .pesections import PEFormatError, read_image, section_bytes


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    os_build = None
    source_label = None
    args = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--os-build" and i + 1 < len(argv):
            os_build = argv[i + 1]
            i += 2
        elif a == "--source-label" and i + 1 < len(argv):
            source_label = argv[i + 1]
            i += 2
        elif a.startswith("-"):
            sys.stderr.write("unknown option: %s\n" % a)
            return 4
        else:
            args.append(a)
            i += 1
    if len(args) != 1:
        sys.stderr.write(__doc__)
        return 4
    path = args[0]
    try:
        image = read_image(path)
        section, sec_meta, is_plus = section_bytes(image, b".apiset")
        parse = parse_namespace(section)
    except UnsupportedVersionError as exc:
        sys.stderr.write("unsupported namespace version: %s\n" % exc)
        return 3
    except (NamespaceFormatError, PEFormatError, BoundsExceeded) as exc:
        sys.stderr.write("format error: %s\n" % exc)
        return 2
    result = build_mapping(parse)
    out = full_report(
        parse, result,
        source_path=source_label or path,
        os_build=os_build,
        image_sha256=hashlib.sha256(image).hexdigest(),
    )
    out["pe32_plus"] = is_plus
    out["mapping_document"] = result.document()
    sys.stdout.write(json.dumps(out, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
