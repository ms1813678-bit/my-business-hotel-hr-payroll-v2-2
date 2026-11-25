# ETW feasibility stopped, 08:07 Cairo, 25 November 2026

Recording remains explicitly disabled at record_once entry. No ETW session started, provider enabled, or baseline parser launched by this work. Query-only native compilation succeeded in run45556792220; that is not recording qualification.

Independent review repair1 residual event-tree memory defect reproduced:300k children inside one Event,1.2MB input,62.8MB peak. Final bounded local input repair adds96element per-event cap. Same reproducer now refuses at about1.2MB peak.15distinct test methods pass, with inherited duplicate tests removed. Non-string output path now refuses before basename, although unreachable through current decoder. These are parser hygiene repairs, not native trace acceptance.

No more ETW implementation iterations planned. Unresolved baseline caller/control generation, circular overwrite/completeness, post-ready watchdog liveness and timeout/root-deletion race, actual owner-crash session/raw cleanup, path normalization and real tracerpt schema remain execution blockers. Some Windows recollections in review are unverified; installed manifest expressly names Status and ThreadId, so alternative recollected names are not adopted as defects. The source remains non-executable by design, unqualified and unsuitable for enabling.

Pivot agreed by parent at08:05:46Cairo: research local zero-additional-cost containment alternatives without weakening isolation or making the test suite pass by bypass.
