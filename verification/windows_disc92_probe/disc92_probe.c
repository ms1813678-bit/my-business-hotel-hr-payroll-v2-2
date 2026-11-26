/* disc92 diagnostic probe only. Static import: kernel32 only. No CRT. No output channel except the exit code.
 * Runs in the staged runtime directory in place of python.exe, through the unchanged launcher, inside the
 * unchanged AppContainer. Every step runs even after an earlier failure. All paths are absolute and are built
 * from the directory of this exe. The stager made four byte-patched copies of the pinned python314.dll
 * (sha256 pinned, verified before launch): pydisc92_v0, _a, _b, _c. Each has the PE checksum zeroed, the resource
 * directory intact and every length unchanged.
 *  C1  LoadLibraryExW(vcruntime140.dll, DONT_RESOLVE_DLL_REFERENCES), free        (control)
 *  C2  LoadLibraryExW(libffi-8.dll, 0), free                                       (control)
 *  S8a LoadLibraryExW(python314.dll, 0) baseline, expected error 5, class recorded
 *  LDR ntdll!LdrLoadDll(python314.dll) baseline, exact NTSTATUS class recorded
 *  V0  checksum zeroed only                                                        (control, expected to fail)
 *  B   manifest: one comment character changed, same length                        (control, expected to fail)
 *  S8b python314.dll again (baseline repeated), class recorded
 *  C   RT_VERSION data zeroed in place, same length
 *  A   manifest: the Common-Controls dependency element replaced by spaces, same length (last)
 * Full loads (flags 0) only, no free. Exit code = 0x20000000 | fields; bits 29+ only the marker; TerminateProcess only.
 *  bit 0 C1, 1 C2, 2 S8a, 3 V0, 4 A, 5 B, 6 C, 7 S8b  (1 = ok)
 *  bits 8-9 error class S8a, 10-11 V0, 12-13 A, 14-15 B, 16-17 C, 18-19 S8b
 *      0 ok, 1 error 5, 2 one of 1114/126/127/193, 3 other
 *  bits 20-22 NTSTATUS class of the LdrLoadDll baseline: 0 ok, 1 0xC0000022, 2 0xC0000142, 3 0xC0000135, 4 other
 *  bits 23-28 zero. Exactly 0x20000000 = path construction refused before any step (no result).
 * Result is a positive-only discriminator. Not Python, not a parser, no acceptance. */
#include <windows.h>

static unsigned int bits = 0;
static unsigned int cls[6];
static unsigned int ntclass = 0;
static wchar_t dir_[520];
static unsigned int dir_len = 0;

static unsigned int classify(unsigned int err)
{
    if (err == 5u) return 1u;
    if (err == 1114u || err == 126u || err == 127u || err == 193u) return 2u;
    return 3u;
}

static int build(wchar_t *out, const wchar_t *name)
{
    unsigned int i = 0, j = 0;
    while (name[j] != 0) j++;
    if (dir_len + j + 1u > 500u) return 0;
    for (i = 0; i < dir_len; i++) out[i] = dir_[i];
    for (j = 0; name[j] != 0; j++) out[i + j] = name[j];
    out[i + j] = 0;
    return 1;
}

/* Full path load. free_it nonzero: FreeLibrary on success. Records bit and class at slot (class slots 2..8). */
static void load_abs(const wchar_t *name, DWORD flags, int free_it, unsigned int bit, int slot)
{
    wchar_t path[520];
    HMODULE h;
    if (!build(path, name)) { if (slot >= 0) cls[slot] = 3u; return; }
    h = LoadLibraryExW(path, NULL, flags);
    if (h == NULL) {
        if (slot >= 0) cls[slot] = classify(GetLastError());
        return;
    }
    bits |= 1u << bit;
    if (free_it) FreeLibrary(h);
}

typedef struct { unsigned short Length; unsigned short MaximumLength; wchar_t *Buffer; } ustr_t;
typedef LONG (NTAPI *ldr_load_t)(PWSTR, PULONG, ustr_t *, PVOID *);

static void ldr_baseline(void)
{
    wchar_t path[520];
    HMODULE nt;
    ldr_load_t fn;
    ustr_t name;
    PVOID base = NULL;
    unsigned int n = 0;
    unsigned int st;
    ntclass = 4u;
    if (!build(path, L"python314.dll")) return;
    nt = GetModuleHandleW(L"ntdll.dll");
    if (nt == NULL) return;
    fn = (ldr_load_t)GetProcAddress(nt, "LdrLoadDll");
    if (fn == NULL) return;
    while (path[n] != 0) n++;
    name.Length = (unsigned short)(n * 2u);
    name.MaximumLength = (unsigned short)(n * 2u + 2u);
    name.Buffer = path;
    st = (unsigned int)fn(NULL, NULL, &name, &base);
    if ((st & 0x80000000u) == 0u) ntclass = 0u;
    else if (st == 0xC0000022u) ntclass = 1u;
    else if (st == 0xC0000142u) ntclass = 2u;
    else if (st == 0xC0000135u) ntclass = 3u;
    else ntclass = 4u;
}

void mainCRTStartup(void)
{
    wchar_t self[520];
    DWORD n, k;

    n = GetModuleFileNameW(NULL, self, 520u);
    if (n == 0 || n >= 500u) TerminateProcess(GetCurrentProcess(), 0x20000000u);
    while (n > 0 && self[n - 1] != L'\\') n--;
    if (n == 0) TerminateProcess(GetCurrentProcess(), 0x20000000u);
    for (k = 0; k < n; k++) dir_[k] = self[k];
    dir_[n] = 0;
    dir_len = n;
    for (k = 0; k < 6; k++) cls[k] = 0;

    load_abs(L"vcruntime140.dll", 0x1u, 1, 0, -1);                 /* C1 */
    load_abs(L"libffi-8.dll", 0u, 1, 1, -1);                       /* C2 */
    load_abs(L"python314.dll", 0u, 0, 2, 0);                       /* S8a  class slot 0 */
    ldr_baseline();                                                /* LDR */
    load_abs(L"pydisc92_v0.dll", 0u, 0, 3, 1);                     /* V0 */
    load_abs(L"pydisc92_b.dll", 0u, 0, 5, 3);                      /* B */
    load_abs(L"python314.dll", 0u, 0, 7, 5);                       /* S8b */
    load_abs(L"pydisc92_c.dll", 0u, 0, 6, 4);                      /* C */
    load_abs(L"pydisc92_a.dll", 0u, 0, 4, 2);                      /* A */

    /* TerminateProcess, not ExitProcess: a loaded module may have run DllMain, and ExitProcess would run
     * DLL_PROCESS_DETACH without Python init, where a crash could hide this result. */
    TerminateProcess(GetCurrentProcess(), 0x20000000u | bits | (cls[0] << 8) | (cls[1] << 10) | (cls[2] << 12)
                     | (cls[3] << 14) | (cls[4] << 16) | (cls[5] << 18) | (ntclass << 20));
}
