/* Diagnostic forwarder probe only. Static import: kernel32 only. No CRT.
 * Runs in the staged runtime directory in place of python.exe, through the unchanged launcher.
 * "targets.h" is generated on the runner by forwarders.py from dumpbin text (names validated, 1..20).
 *  P0 (bit 22) baseline LoadLibraryExW(L"python314.dll", NULL, 0). Expected to fail. Freed if it loads.
 *  P1 (bits 0..19) each target: full-resolution LoadLibraryExW(name, NULL, 0), FreeLibrary on success.
 *  P2 (bit 20) full path of python314.dll in this exe's directory,
 *     LOAD_LIBRARY_SEARCH_DEFAULT_DIRS | LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR. Freed on success.
 *  P3 (bit 21) same path with LOAD_WITH_ALTERED_SEARCH_PATH. Freed on success.
 * Exit code = 0x20000000 | success bits (0..22) | first failing P1 target, 1-based, 0 if none (bits 23..27).
 * Bit 28 is spare and never set. Bits 30,31 never set. A set bit means the load worked.
 * Every step runs even after an earlier failure. Exit by TerminateProcess. No error code is recorded.
 * Not Python, not a parser, no acceptance. */
#include <windows.h>
#include "targets.h"

#ifndef LOAD_LIBRARY_SEARCH_DEFAULT_DIRS
#define LOAD_LIBRARY_SEARCH_DEFAULT_DIRS 0x1000
#endif
#ifndef LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR
#define LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR 0x100
#endif

#define SEARCH_FLAGS_P2 (LOAD_LIBRARY_SEARCH_DEFAULT_DIRS | LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR)

static unsigned int mask = 0;

static int load_free(const wchar_t *name, DWORD flags)
{
    HMODULE h = LoadLibraryExW(name, NULL, flags);
    if (h == NULL) {
        return 0;
    }
    FreeLibrary(h);
    return 1;
}

void mainCRTStartup(void)
{
    static const wchar_t py_name[] = L"python314.dll";
    wchar_t path[320];
    unsigned int k, len, dir_len = 0, i, first_fail = 0;

    if (load_free(py_name, 0)) mask |= 1u << 22;

    for (k = 0; k < TARGET_COUNT; k++) {
        if (load_free(targets[k], 0)) {
            mask |= 1u << k;
        } else if (first_fail == 0) {
            first_fail = k + 1;
        }
    }

    len = GetModuleFileNameW(NULL, path, 300);
    if (len > 0 && len < 280) {
        for (k = 0; k < len; k++) if (path[k] == L'\\') dir_len = k + 1;
    }
    if (dir_len != 0) {
        for (i = 0; py_name[i] != 0; i++) path[dir_len + i] = py_name[i];
        path[dir_len + i] = 0;
        if (load_free(path, SEARCH_FLAGS_P2)) mask |= 1u << 20;
        if (load_free(path, LOAD_WITH_ALTERED_SEARCH_PATH)) mask |= 1u << 21;
    }

    TerminateProcess(GetCurrentProcess(), 0x20000000u | mask | (first_fail << 23));
}
