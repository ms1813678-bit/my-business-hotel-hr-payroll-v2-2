/* Diagnostic startup probe only. Static import: kernel32 only. No CRT.
 * Runs in the staged runtime directory in place of python.exe, through the unchanged launcher.
 * Steps, all in order, each independent where possible, none runs Python code:
 *  1 LoadLibraryExW(python314.dll) WITH import resolution and DllMain
 *  2 GetProcAddress(Py_GetVersion)
 *  3 call Py_GetVersion (no Py_Initialize), expect a string starting with '3'
 *  4 BCryptGenRandom via LoadLibrary/GetProcAddress, system-preferred RNG
 *  5 open+read 1 byte of python314.zip   6 open+read 1 byte of python314._pth
 *  7 open+read 1 byte of candidate_worker.py   (all three by path in this exe's directory)
 *  8-10 GetFileType on stdin, stdout, stderr must not be unknown
 * Exit code = 0x20000000 | steps 1..10 success bits (bits 0..9)
 *           | first failing step number 1..10, 0 if none (bits 10..13)
 *           | low 15 bits of that step's error (bits 14..28). Bits 30,31 never set.
 * Error is GetLastError for steps 1,2,5-10 and the NTSTATUS low 15 bits for step 3/4 status.
 * Exit is by TerminateProcess(GetCurrentProcess()), see the end of mainCRTStartup.
 * Not Python, not a parser, no security or adapter acceptance. */
#include <windows.h>

typedef const char *(__cdecl *PyGetVersionFn)(void);
typedef LONG (WINAPI *BCryptGenRandomFn)(void *, unsigned char *, ULONG, ULONG);

static unsigned int mask = 0;
static unsigned int first_fail = 0;
static unsigned int first_err = 0;

static void step(unsigned int number, int ok, unsigned int err)
{
    if (ok) {
        mask |= 1u << (number - 1);
    } else if (first_fail == 0) {
        first_fail = number;
        first_err = err & 0x7FFFu;
    }
}

static int read_one(const wchar_t *dir, unsigned int dir_len, const wchar_t *name, unsigned int *err)
{
    wchar_t path[320];
    unsigned int i = 0;
    HANDLE h;
    unsigned char byte = 0;
    DWORD got = 0;
    int ok;
    for (; i < dir_len; i++) path[i] = dir[i];
    for (; name[0] != 0 && i < 319; name++, i++) path[i] = name[0];
    path[i] = 0;
    h = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) {
        *err = GetLastError();
        return 0;
    }
    ok = ReadFile(h, &byte, 1, &got, NULL) && got == 1;
    if (!ok) *err = GetLastError();
    CloseHandle(h);
    return ok;
}

void mainCRTStartup(void)
{
    wchar_t self[320];
    unsigned int len = 0, dir_len = 0, err = 0;
    HMODULE py = NULL;
    FARPROC version = NULL;
    HMODULE bcrypt = NULL;
    FARPROC gen = NULL;
    unsigned char random[16];
    unsigned int k;
    DWORD type;
    HANDLE std[3];

    py = LoadLibraryExW(L"python314.dll", NULL, 0);
    err = GetLastError();
    step(1, py != NULL, err);

    if (py != NULL) {
        version = GetProcAddress(py, "Py_GetVersion");
        err = GetLastError();
    }
    step(2, version != NULL, err);
    if (version != NULL) {
        const char *text = ((PyGetVersionFn)version)();
        step(3, text != NULL && text[0] == '3', ERROR_INVALID_DATA);
    } else {
        step(3, 0, err);
    }

    bcrypt = LoadLibraryExW(L"bcrypt.dll", NULL, 0);
    err = GetLastError();
    if (bcrypt != NULL) {
        gen = GetProcAddress(bcrypt, "BCryptGenRandom");
        err = GetLastError();
    }
    if (gen != NULL) {
        LONG status = ((BCryptGenRandomFn)gen)(NULL, random, sizeof(random), 0x2);
        step(4, status == 0, (unsigned int)status);
    } else {
        step(4, 0, err);
    }

    len = GetModuleFileNameW(NULL, self, 320);
    if (len > 0 && len < 300) {
        for (k = 0; k < len; k++) if (self[k] == L'\\') dir_len = k + 1;
    }
    if (dir_len == 0) {
        step(5, 0, ERROR_BAD_PATHNAME);
        step(6, 0, ERROR_BAD_PATHNAME);
        step(7, 0, ERROR_BAD_PATHNAME);
    } else {
        int ok5, ok6, ok7;
        unsigned int e5 = 0, e6 = 0, e7 = 0;
        /* Result and error are separate statements: argument evaluation order is unspecified in C. */
        ok5 = read_one(self, dir_len, L"python314.zip", &e5);
        step(5, ok5, e5);
        ok6 = read_one(self, dir_len, L"python314._pth", &e6);
        step(6, ok6, e6);
        ok7 = read_one(self, dir_len, L"candidate_worker.py", &e7);
        step(7, ok7, e7);
    }

    std[0] = GetStdHandle(STD_INPUT_HANDLE);
    std[1] = GetStdHandle(STD_OUTPUT_HANDLE);
    std[2] = GetStdHandle(STD_ERROR_HANDLE);
    for (k = 0; k < 3; k++) {
        type = FILE_TYPE_UNKNOWN;
        err = ERROR_INVALID_HANDLE;
        if (std[k] != NULL && std[k] != INVALID_HANDLE_VALUE) {
            SetLastError(0);
            type = GetFileType(std[k]);
            err = GetLastError();
        }
        step(8 + k, type != FILE_TYPE_UNKNOWN, err);
    }

    /* TerminateProcess, not ExitProcess: python314.dll is loaded with DllMain, and ExitProcess would run
     * DLL_PROCESS_DETACH without Python init, where a crash could hide this result. */
    TerminateProcess(GetCurrentProcess(), 0x20000000u | mask | (first_fail << 10) | (first_err << 14));
}
