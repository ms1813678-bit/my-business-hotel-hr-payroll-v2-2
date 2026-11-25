# Repair1 source-only, 08:00

Recording entry is explicitly disabled. This revision is not an execution proposal.

Reproduced original0754 missing-GUID, duplicate-System-field and junk-memory findings. Added fail-closed GUID/system uniqueness/XML structure/depth/text/attribute caps, strict integer loss type, independent per-session stop/query even on first timeout, owner-synchronize handle before readiness, fixed-path known-raw deletion after normal completion or parent death, separate stop and conversion-complete handshakes, child/control correlation and sanitized return.17synthetic test methods pass, including first stop timeout, bad first query, independent raw cleanup and 300k junk XML early refusal under2MB peak.

Still unqualified: actual owner-crash native cleanup/races and provider control/ETL reality; exact baseline caller is not implemented; circular overwrite detection and capture completeness are not established; a known control is necessary but cannot prove completeness. No source repair is treated as native acceptance. Original0754 archive remains the reviewed immutable source; this is a new revision.

# Review-only trace implementation, 07:54

No recording, provider enablement or parser launch has occurred. None of this directory is wired into a workflow. This is not a ready-to-execute preview.

Implemented review source:
- `correlate.py`: normalized exact process identity and lifetime, thread lifetimes, IRP completion correlation, loss/ambiguity refusal. Results remain local and include raw paths until a separate output filter exists.
- `xml_adapter.py`: bounded streaming tracerpt event XML parsing, exact UTC nanosecond integer timestamps, installed provider/event/version gates, required manifest fields, unsigned integer checks, duplicate/DTD/entity refusal. Synthetic tests only.
- `session_probe.c`: no start or enable path. Query fixed descriptive session names, stop only captured matching handle or matching fresh-root ETL path, return numeric loss statistics and verify absence. Not yet natively compiled.
- `watchdog.py`: independent process, readiness marker before starting sessions, prearmed journal of both exact owned ETL paths, bounded stop/query, result marker. Journal is prearmed to cover owner death between start and handle-journal write.
- `controller.py`: fixed two names with absent-only entry, exact provider keyword argv, 64MB circular files /64KB buffers/16..64, five-second scope, ten-second watchdog, local bounded conversion, cleanup of direct entries from fresh owned root. No CLI entrypoint.

Six synthetic test methods pass with 24 refusal subcases. The native controller/watchdog are unqualified; synthetic correlation passing does not establish correct ETL payloads, session containment, cleanup or parser cause.

Still required before execution preview:
1. Native compile and query-only qualification. No session recording needed for that check.
2. Source-pinned caller that invokes the exact nondebugger baseline once and owns the launcher PID. Baseline native diagnostics give the created PID but not child creation timestamp. Recover creation from unique ETW process start only when PID + exact launcher parent + exact staged image + bounded owner interval agree. This evidence construction is not yet implemented. Do not silently use current debugger/IFEO source.
3. Known synthetic missing-open/control in the same provider capture and refusal on absent control.
4. Sanitized output filtering: permitted synthetic target basename or keyed path hash, status/counts only. Never return raw XML/ETL, command stderr or arbitrary paths.
5. Crash/race tests for prestart, one-session-started, workload exception, owner death and decoder failure. Ensure another session cannot be stopped on collision/replacement.
6. Loss statistics and exact session absence must be independently verified. Missing stats are refusal, even if traces contain useful-looking events.

Exact baseline remains efb977987a747a5a7d848ff6ed4a58249af91736, launcher SHA256 7e041ac096efa68c42a6556752e53f1b7715e1df368ca894499da72be1b6e402. No baseline files are modified by these modules. One unchanged-startup recording maximum, then pivot if unavailable/inconclusive. A rejected open alone is not proof of the fatal loader cause. Network denial remains separate.

Sources:
- https://learn.microsoft.com/en-us/windows/win32/api/evntrace/nf-evntrace-controltracew
- https://learn.microsoft.com/en-us/windows/win32/api/evntrace/ns-evntrace-event_trace_properties
- https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/tracerpt
- Installed manifest evidence https://github.com/ms1813678-bit/my-business-hotel-hr-payroll-v2-2/actions/runs/45554142230
