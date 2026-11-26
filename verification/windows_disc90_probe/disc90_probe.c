/* disc90 diagnostic probe only. Static import: kernel32 only. No CRT. No output channel except the exit code.
 * Runs in the staged runtime directory in place of python.exe, through the unchanged launcher, inside the
 * unchanged AppContainer. Every step runs even after an earlier failure. All paths are absolute and are
 * built from the directory of this exe (the staged runtime directory).
 *  C1  LoadLibraryExW(vcruntime140.dll, DONT_RESOLVE_DLL_REFERENCES), free        (control)
 *  C2  LoadLibraryExW(libffi-8.dll, 0), free                                       (control)
 *  S2  python314.dll LOAD_LIBRARY_AS_DATAFILE, free
 *  S3  python314.dll LOAD_LIBRARY_AS_IMAGE_RESOURCE, free
 *  S4  python314.dll DONT_RESOLVE_DLL_REFERENCES, free                             (key step)
 *  S6  READ_CONTROL on python314.dll and vcruntime140.dll via dynamically loaded advapi32:
 *      a both open and readable   b DACL bytes equal   c owner SID equal
 *      d python314.dll has a mandatory label ACE   e label ACE state equal (both absent, or identical bytes)
 *      f CreateFileW(python314.dll, FILE_GENERIC_EXECUTE|FILE_GENERIC_READ, share read) succeeds.
 *      Nothing from the descriptors is printed or returned except these bits.
 *  S8a LoadLibraryExW(python314.dll, 0)                  baseline, expected to fail with error 5
 *  S7  LoadLibraryExW(name, 0), no free, for each of the 19 direct imports of python314.dll in import-table order
 *  S8b LoadLibraryExW(python314.dll, 0) again
 *  S5  LoadLibraryExW(pydisc314.dll, 0), last, no free (byte copy of python314.dll made by the stager)
 * Exit code = 0x20000000 | fields. Bits 30 and 31 never set. TerminateProcess is the only exit.
 *  bit 0 C1, 1 C2, 2 S2, 3 S3, 4 S4, 5 S6a, 6 S6b, 7 S6c, 8 S6d, 9 S6e, 10 S6f, 11 S8a, 12 S8b, 13 S5  (1 = ok)
 *  bits 14-15 error class of S2, 16-17 S3, 18-19 S4, 20-21 S8a, 22-23 S5
 *      0 ok, 1 error 5, 2 one of 1114/126/127/193, 3 other
 *  bits 24-28 index (1..19) of the first S7 import that failed, 0 if none
 *  Exactly 0x20000000 = path construction refused before any step (no result).
 * Result is a positive-only discriminator. Not Python, not a parser, no acceptance. */
#include <windows.h>

static unsigned int bits = 0;
static unsigned int cls_s2 = 0, cls_s3 = 0, cls_s4 = 0, cls_s8a = 0, cls_s5 = 0, s7_first = 0;

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

/* Full path load. free_it nonzero: FreeLibrary on success. Returns 1 if the load worked; *cls is set on failure. */
static int load_abs(const wchar_t *name, DWORD flags, int free_it, unsigned int *cls)
{
    wchar_t path[520];
    HMODULE h;
    DWORD err;
    if (!build(path, name)) { *cls = 3u; return 0; }
    h = LoadLibraryExW(path, NULL, flags);
    if (h == NULL) { err = GetLastError(); *cls = classify(err); return 0; }
    if (free_it) FreeLibrary(h);
    return 1;
}

typedef DWORD (WINAPI *get_sec_t)(HANDLE, DWORD, DWORD, PSID *, PSID *, PACL *, PACL *, PVOID *);
typedef BOOL (WINAPI *get_acl_info_t)(PACL, LPVOID, DWORD, DWORD);
typedef BOOL (WINAPI *equal_sid_t)(PSID, PSID);
typedef BOOL (WINAPI *get_ace_t)(PACL, DWORD, LPVOID *);
typedef DWORD (WINAPI *sid_len_t)(PSID);

typedef struct { PSID owner; PACL dacl; PACL sacl; PVOID sd; HANDLE h; int ok; } sec_t;

static int same_bytes(const unsigned char *a, const unsigned char *b, unsigned int n)
{
    unsigned int i;
    for (i = 0; i < n; i++) if (a[i] != b[i]) return 0;
    return 1;
}

static void read_sec(const wchar_t *name, get_sec_t gs, sec_t *s)
{
    wchar_t path[520];
    s->owner = NULL; s->dacl = NULL; s->sacl = NULL; s->sd = NULL; s->h = INVALID_HANDLE_VALUE; s->ok = 0;
    if (!build(path, name)) return;
    s->h = CreateFileW(path, READ_CONTROL, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (s->h == INVALID_HANDLE_VALUE) return;
    /* 1 = SE_FILE_OBJECT; 0x1 owner, 0x4 DACL, 0x10 mandatory label */
    if (gs(s->h, 1u, 0x1u | 0x4u | 0x10u, &s->owner, NULL, &s->dacl, &s->sacl, &s->sd) != 0u) return;
    if (s->owner == NULL || s->dacl == NULL) return;
    s->ok = 1;
}

/* Returns 1 and the ACE pointer/size if the SACL holds a SYSTEM_MANDATORY_LABEL_ACE (type 0x11). */
static int label_ace(PACL sacl, get_acl_info_t gai, get_ace_t ga, const unsigned char **ace, unsigned int *size)
{
    ACL_SIZE_INFORMATION info;
    DWORD i;
    LPVOID p;
    if (sacl == NULL) return 0;
    if (!gai(sacl, &info, (DWORD)sizeof(info), 2u)) return 0;
    for (i = 0; i < info.AceCount; i++) {
        if (!ga(sacl, i, &p)) return 0;
        if (((const unsigned char *)p)[0] == 0x11u) {
            *ace = (const unsigned char *)p;
            *size = (unsigned int)((const unsigned char *)p)[2] | ((unsigned int)((const unsigned char *)p)[3] << 8);
            return 1;
        }
    }
    return 0;
}

static void acl_dump(void)
{
    HMODULE adv;
    get_sec_t gs;
    get_acl_info_t gai;
    equal_sid_t eq;
    get_ace_t ga;
    sec_t a, b;
    HANDLE exec;
    wchar_t path[520];
    ACL_SIZE_INFORMATION ia, ib;
    const unsigned char *la = NULL, *lb = NULL;
    unsigned int sa = 0, sb = 0;
    int pa, pb;

    adv = LoadLibraryExW(L"advapi32.dll", NULL, 0x800u); /* LOAD_LIBRARY_SEARCH_SYSTEM32 */
    if (adv != NULL) {
        gs = (get_sec_t)GetProcAddress(adv, "GetSecurityInfo");
        gai = (get_acl_info_t)GetProcAddress(adv, "GetAclInformation");
        eq = (equal_sid_t)GetProcAddress(adv, "EqualSid");
        ga = (get_ace_t)GetProcAddress(adv, "GetAce");
        if (gs != NULL && gai != NULL && eq != NULL && ga != NULL) {
            read_sec(L"python314.dll", gs, &a);
            read_sec(L"vcruntime140.dll", gs, &b);
            if (a.ok && b.ok) {
                bits |= 1u << 5;
                if (eq(a.owner, b.owner)) bits |= 1u << 7;
                if (gai(a.dacl, &ia, (DWORD)sizeof(ia), 2u) && gai(b.dacl, &ib, (DWORD)sizeof(ib), 2u)
                    && ia.AclBytesInUse == ib.AclBytesInUse
                    && same_bytes((const unsigned char *)a.dacl, (const unsigned char *)b.dacl, ia.AclBytesInUse))
                    bits |= 1u << 6;
                pa = label_ace(a.sacl, gai, ga, &la, &sa);
                pb = label_ace(b.sacl, gai, ga, &lb, &sb);
                if (pa) bits |= 1u << 8;
                if ((!pa && !pb) || (pa && pb && sa == sb && same_bytes(la, lb, sa))) bits |= 1u << 9;
            }
            if (a.sd != NULL) LocalFree(a.sd);
            if (b.sd != NULL) LocalFree(b.sd);
            if (a.h != INVALID_HANDLE_VALUE) CloseHandle(a.h);
            if (b.h != INVALID_HANDLE_VALUE) CloseHandle(b.h);
        }
        FreeLibrary(adv);
    }
    if (build(path, L"python314.dll")) {
        exec = CreateFileW(path, FILE_GENERIC_EXECUTE | FILE_GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
        if (exec != INVALID_HANDLE_VALUE) { bits |= 1u << 10; CloseHandle(exec); }
    }
}

void mainCRTStartup(void)
{
    static const wchar_t *const imports[19] = {
        L"VERSION.dll", L"WS2_32.dll", L"api-ms-win-core-path-l1-1-0.dll", L"bcrypt.dll", L"ADVAPI32.dll",
        L"KERNEL32.dll", L"VCRUNTIME140.dll", L"api-ms-win-crt-stdio-l1-1-0.dll", L"api-ms-win-crt-environment-l1-1-0.dll",
        L"api-ms-win-crt-string-l1-1-0.dll", L"api-ms-win-crt-runtime-l1-1-0.dll", L"api-ms-win-crt-heap-l1-1-0.dll",
        L"api-ms-win-crt-locale-l1-1-0.dll", L"api-ms-win-crt-convert-l1-1-0.dll", L"api-ms-win-crt-math-l1-1-0.dll",
        L"api-ms-win-crt-time-l1-1-0.dll", L"api-ms-win-crt-process-l1-1-0.dll", L"api-ms-win-crt-conio-l1-1-0.dll",
        L"api-ms-win-crt-filesystem-l1-1-0.dll"
    };
    wchar_t self[520];
    DWORD n, k;
    unsigned int cls = 0;
    int ok;

    n = GetModuleFileNameW(NULL, self, 520u);
    if (n == 0 || n >= 500u) TerminateProcess(GetCurrentProcess(), 0x20000000u);
    while (n > 0 && self[n - 1] != L'\\') n--;
    if (n == 0) TerminateProcess(GetCurrentProcess(), 0x20000000u);
    for (k = 0; k < n; k++) dir_[k] = self[k];
    dir_[n] = 0;
    dir_len = n;

    cls = 0; ok = load_abs(L"vcruntime140.dll", 0x1u, 1, &cls); if (ok) bits |= 1u << 0;
    cls = 0; ok = load_abs(L"libffi-8.dll", 0u, 1, &cls); if (ok) bits |= 1u << 1;
    cls = 0; ok = load_abs(L"python314.dll", 0x2u, 1, &cls); if (ok) bits |= 1u << 2; else cls_s2 = cls;
    cls = 0; ok = load_abs(L"python314.dll", 0x20u, 1, &cls); if (ok) bits |= 1u << 3; else cls_s3 = cls;
    cls = 0; ok = load_abs(L"python314.dll", 0x1u, 1, &cls); if (ok) bits |= 1u << 4; else cls_s4 = cls;

    acl_dump();

    cls = 0; ok = load_abs(L"python314.dll", 0u, 0, &cls); if (ok) bits |= 1u << 11; else cls_s8a = cls;

    for (k = 0; k < 19; k++) {
        /* bare name, flags 0: the same search the loader uses for the imports of python314.dll */
        ok = (LoadLibraryExW(imports[k], NULL, 0u) != NULL);
        if (!ok && s7_first == 0) s7_first = k + 1u;
    }

    cls = 0; ok = load_abs(L"python314.dll", 0u, 0, &cls); if (ok) bits |= 1u << 12;

    cls = 0; ok = load_abs(L"pydisc314.dll", 0u, 0, &cls); if (ok) bits |= 1u << 13; else cls_s5 = cls;

    /* TerminateProcess, not ExitProcess: python314.dll may be loaded and DllMain run; ExitProcess would run
     * DLL_PROCESS_DETACH without Python init, where a crash could hide this result. */
    TerminateProcess(GetCurrentProcess(), 0x20000000u | bits | (cls_s2 << 14) | (cls_s3 << 16) | (cls_s4 << 18)
                     | (cls_s8a << 20) | (cls_s5 << 22) | (s7_first << 24));
}
