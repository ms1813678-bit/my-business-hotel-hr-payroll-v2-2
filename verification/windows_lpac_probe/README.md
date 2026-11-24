# Instinct synthetic Windows LPAC probe, not application activation

Fixed synthetic native test only. No application import, database, hotel file, token, secret, cache or artifact upload. New source does not replace rev9 or remove the application Windows guard. Compiler pinned llvm-mingw20261117 with release asset SHA256, using Windows system/UCRT DLLs. LLVM Apache2/exception and llvm-mingw ISC/component terms require final distribution audit; no runtime release is delivered here.

The native program launches only itself with a fixed child probe. Native owner records, JOB_LIST creation-time limited job, LPAC token and exact created-job/SID check before resume; no-network capabilities, minimal SYSTEMROOT-only environment, explicit std handles. Three-second work wait/two-second cleanup wait,1024-byte result cap. Child's file/loopback probes accept only access-denied, not missing file/refused connection. All input is fixed synthetic canary data.

PowerShell harness is restricted to GitHub Actions with RUNNER_TEMP. It creates one fresh random AppContainer profile, runtime and forbidden-canary directories ONLY in that ephemeral runner, removes inherited ACLs on those paths, grants synthetic profile runtime read/execute but not canary access, then deletes profile and files. No existing profile is changed, no firewall/loopback exemption/network/hotel configuration touched. Profile-deletion failure is a failure, not silent cleanup success. Job ends at10minutes, disposable VM is final backstop.

Not yet executed. Cross-compiled locally warnings-as-errors only. This first actual test scope does not claim Internet/LAN/DNS/UDP/COM/IPC denial, arbitrary parser-code containment, Python cold-start, broker death/fault coverage, whole application Windows suite, packaging or hotel acceptance. Those remain item1 proof work, not granted by this probe. Missing API/privilege/ACL/runtime compatibility refuses, no weaker fallback.

Standard public GitHub Windows runner compute is currently free; no larger runner, cache, artifact, package storage, external paid API or billing change. Ordinary existing tests workflow may also run on this branch push and still has the known application platform-guard failures. No test skip for green.

Sources:
https://github.com/mstorsjo/llvm-mingw/releases/tag/20261117
https://github.com/mstorsjo/llvm-mingw/blob/HEAD/LICENSE.txt
https://llvm.org/LICENSE.txt
https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute
https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer
https://docs.github.com/en/billing/concepts/product-billing/github-actions
