/* Diagnostic dependency probe only. Static import: kernel32 only. No CRT.
 * Runs in the staged runtime directory in place of python.exe, through the unchanged launcher.
 * Each step is a FULL-resolution LoadLibraryExW(name, NULL, 0) of one direct dependency of python314.dll
 * (import list computed from the pinned embed zip, no delay imports), then FreeLibrary on success.
 * python314.dll is loaded LAST and is not freed. No Python code runs.
 *  1 version.dll   2 ws2_32.dll   3 advapi32.dll   4 vcruntime140.dll
 *  5 api-ms-win-crt-runtime-l1-1-0.dll   6 api-ms-win-core-path-l1-1-0.dll
 *  7 the other 11 api-ms-win-crt-*-l1-1-0 sets, one representative each, in a fixed order (see EXTRA_NAMES)
 *  8 python314.dll
 * Exit code = 0x20000000
 *   | steps 1..8 success bits           (bits 0..7)
 *   | first failing step number 1..8, 0 if none (bits 8..11)
 *   | low 13 bits of that step's GetLastError   (bits 12..24)
 *   | for step 7 only: 1-based index of the first failing extra name, else 0 (bits 25..28)
 * Bits 29 is the marker. Bits 30,31 never set, so the code never looks like an NTSTATUS.
 * Step 7 succeeds only if all 11 extra names load. Every step runs even after an earlier failure.
 * Exit is by TerminateProcess(GetCurrentProcess()). Not Python, not a parser, no acceptance. */
#include <windows.h>

static unsigned int mask = 0;
static unsigned int first_fail = 0;
static unsigned int first_err = 0;
static unsigned int first_extra = 0;

static void step(unsigned int number, int ok, unsigned int err, unsigned int extra)
{
    if (ok) {
        mask |= 1u << (number - 1);
    } else if (first_fail == 0) {
        first_fail = number;
        first_err = err & 0x1FFFu;
        first_extra = extra & 0xFu;
    }
}

/* Returns 1 if the full load worked. Error is read right after the call. */
static int load_free(const wchar_t *name, unsigned int *err)
{
    HMODULE h = LoadLibraryExW(name, NULL, 0);
    if (h == NULL) {
        *err = GetLastError();
        return 0;
    }
    FreeLibrary(h);
    return 1;
}

void mainCRTStartup(void)
{
    static const wchar_t *const extra_names[11] = {
        L"api-ms-win-crt-convert-l1-1-0.dll",
        L"api-ms-win-crt-environment-l1-1-0.dll",
        L"api-ms-win-crt-filesystem-l1-1-0.dll",
        L"api-ms-win-crt-heap-l1-1-0.dll",
        L"api-ms-win-crt-locale-l1-1-0.dll",
        L"api-ms-win-crt-math-l1-1-0.dll",
        L"api-ms-win-crt-process-l1-1-0.dll",
        L"api-ms-win-crt-stdio-l1-1-0.dll",
        L"api-ms-win-crt-string-l1-1-0.dll",
        L"api-ms-win-crt-time-l1-1-0.dll",
        L"api-ms-win-crt-conio-l1-1-0.dll"
    };
    unsigned int err = 0, k, bad = 0, bad_err = 0;
    int ok;
    HMODULE py;

    err = 0; ok = load_free(L"version.dll", &err); step(1, ok, err, 0);
    err = 0; ok = load_free(L"ws2_32.dll", &err); step(2, ok, err, 0);
    err = 0; ok = load_free(L"advapi32.dll", &err); step(3, ok, err, 0);
    err = 0; ok = load_free(L"vcruntime140.dll", &err); step(4, ok, err, 0);
    err = 0; ok = load_free(L"api-ms-win-crt-runtime-l1-1-0.dll", &err); step(5, ok, err, 0);
    err = 0; ok = load_free(L"api-ms-win-core-path-l1-1-0.dll", &err); step(6, ok, err, 0);

    for (k = 0; k < 11; k++) {
        err = 0;
        ok = load_free(extra_names[k], &err);
        if (!ok && bad == 0) {
            bad = k + 1;
            bad_err = err;
        }
    }
    step(7, bad == 0, bad_err, bad);

    py = LoadLibraryExW(L"python314.dll", NULL, 0);
    err = GetLastError();
    step(8, py != NULL, err, 0);

    /* TerminateProcess, not ExitProcess: python314.dll may be loaded with DllMain run, and ExitProcess
     * would run DLL_PROCESS_DETACH without Python init, where a crash could hide this result. */
    TerminateProcess(GetCurrentProcess(), 0x20000000u | mask | (first_fail << 8) | (first_err << 12) | (first_extra << 25));
}
