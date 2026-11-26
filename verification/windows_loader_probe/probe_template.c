/* Diagnostic probe only. Imports kernel32 only. No CRT, no file/registry/network/process access.
 * Maps each name from the generated probe_list.h with LoadLibraryExW(DONT_RESOLVE_DLL_REFERENCES):
 * no DllMain and no import resolution. Default LoadLibrary search order with the staged runtime
 * as the application directory; KnownDLLs resolve first. It is not a claim about the real loader
 * inside LPAC beyond this one call per name.
 * Exit code = 0x20000000 | one success bit per name. Bits 30 and 31 are never set, so no
 * NTSTATUS-looking value. Not Python, not a parser, no security or adapter acceptance. */
#include <windows.h>
#include "probe_list.h"

#if PROBE_COUNT < 1 || PROBE_COUNT > 28
#error PROBE_COUNT must be 1..28
#endif

void mainCRTStartup(void)
{
    unsigned int mask = 0;
    unsigned int i;
    for (i = 0; i < PROBE_COUNT; i++) {
        HMODULE h = LoadLibraryExW(PROBE_NAMES[i], NULL, DONT_RESOLVE_DLL_REFERENCES);
        if (h != NULL) {
            mask |= 1u << i;
            FreeLibrary(h);
        }
    }
    ExitProcess(0x20000000u | mask);
}
