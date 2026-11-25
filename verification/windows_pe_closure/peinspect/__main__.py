"""CLI: python -m peinspect parse FILE | python -m peinspect closure ..."""
import json
import sys

from . import closure as _closure
from .pe import parse_file, MACHINE_NAMES


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python -m peinspect {parse FILE | closure ...}", file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "parse":
        if len(rest) != 1:
            print("usage: python -m peinspect parse FILE", file=sys.stderr)
            return 2
        image = parse_file(rest[0])
        out = {
            "machine": image.machine_name,
            "characteristics": "0x%04x" % image.characteristics,
            "imports": [
                {"dll": m.dll_name, "kind": m.kind,
                 "symbols": [
                     ({"name": s.name, "hint": s.hint} if s.name is not None
                      else {"ordinal": s.ordinal})
                     for s in m.symbols]}
                for m in image.imports + image.delay_imports
            ],
            "export_dll_name": image.export_dll_name,
            "exports": [
                {"ordinal": e.ordinal, "name": e.name, "forwarder": e.forwarder}
                for e in image.exports
            ],
        }
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    if cmd == "closure":
        return _closure.main(rest)
    print("unknown command %r" % cmd, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
