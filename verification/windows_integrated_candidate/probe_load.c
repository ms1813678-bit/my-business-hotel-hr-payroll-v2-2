/* Diagnostic only. Same-token loader isolation child. Not parser code and not acceptance.
 * Imports kernel32 only (build with /MT). Reports fixed labels and Win32 error numbers.
 * No paths, no module text and no environment values are printed. */
#include <windows.h>

static void emit(const char *text, DWORD length) {
  HANDLE out = GetStdHandle(STD_OUTPUT_HANDLE);
  DWORD written = 0;
  if (out && out != INVALID_HANDLE_VALUE) (void)WriteFile(out, text, length, &written, NULL);
}

static DWORD put(char *dst, DWORD at, const char *src) {
  while (*src) dst[at++] = *src++;
  return at;
}

static DWORD put_number(char *dst, DWORD at, DWORD value) {
  char digits[11];
  DWORD count = 0;
  do {
    digits[count++] = (char)('0' + value % 10);
    value /= 10;
  } while (value);
  while (count) dst[at++] = digits[--count];
  return at;
}

typedef LONG (NTAPI *last_status_fn)(void);

/* Resolved once in wmain BEFORE any measured load, so no lookup runs between a failed load and the reads.
   Capture order after a failed load: GetLastError first, then this cached pointer. Thread last status is a label, not a root cause. */
static last_status_fn nt_status_fn;
static void resolve_nt_status(void) {
  HMODULE ntdll = GetModuleHandleW(L"ntdll.dll");
  if (ntdll) nt_status_fn = (last_status_fn)GetProcAddress(ntdll, "RtlGetLastNtStatus");
}

static void report(const char *label, HMODULE module, DWORD error, DWORD status, int have_status) {
  char line[128];
  DWORD at = put(line, 0, "probe_load label=");
  at = put(line, at, label);
  at = put(line, at, module ? " ok=1 error=0" : " ok=0 error=");
  if (!module) {
    at = put_number(line, at, error);
    at = put(line, at, " nt=");
    for (int shift = 28; shift >= 0; shift -= 4) line[at++] = "0123456789abcdef"[(status >> shift) & 15];
    at = put(line, at, have_status ? " ntavail=1" : " ntavail=0");
  }
  line[at++] = '\n';
  emit(line, at);
}

static void try_system(const char *label, const wchar_t *name) {
  SetLastError(0);
  HMODULE module = LoadLibraryExW(name, NULL, LOAD_LIBRARY_SEARCH_SYSTEM32);
  DWORD error = module ? 0 : GetLastError();
  DWORD status = (!module && nt_status_fn) ? (DWORD)nt_status_fn() : 0;
  report(label, module, error, status, nt_status_fn != NULL);
  if (module) (void)FreeLibrary(module);
}

static void try_staged(const char *label, const wchar_t *directory, const wchar_t *name, DWORD flags, int keep) {
  wchar_t path[MAX_PATH + 32];
  size_t used = 0;
  while (directory[used] && used < MAX_PATH) {
    path[used] = directory[used];
    used++;
  }
  for (size_t i = 0; name[i] && used < MAX_PATH + 30; i++) path[used++] = name[i];
  path[used] = 0;
  SetLastError(0);
  HMODULE module = LoadLibraryExW(path, NULL, flags);
  DWORD error = module ? 0 : GetLastError();
  DWORD status = (!module && nt_status_fn) ? (DWORD)nt_status_fn() : 0;
  report(label, module, error, status, nt_status_fn != NULL);
  if (module && !keep) (void)FreeLibrary(module);
}

int wmain(void) {
  wchar_t self[MAX_PATH + 1] = {0};
  DWORD length = GetModuleFileNameW(NULL, self, MAX_PATH);
  resolve_nt_status();
  emit("probe_start ok=1\n", 17);
  if (!length || length >= MAX_PATH) {
    emit("probe_self ok=0\n", 16);
    return 3;
  }
  size_t slash = length;
  while (slash && self[slash - 1] != L'\\') slash--;
  self[slash] = 0;
  /* Fixed list of python314.dll's documented-family system dependencies, one at a time. */
  try_system("ucrtbase", L"ucrtbase.dll");
  try_system("advapi32", L"advapi32.dll");
  try_system("shell32", L"shell32.dll");
  try_system("ole32", L"ole32.dll");
  try_system("oleaut32", L"oleaut32.dll");
  try_system("version", L"version.dll");
  try_system("user32", L"user32.dll");
  try_system("bcrypt", L"bcrypt.dll");
  try_system("ws2_32", L"ws2_32.dll");
  /* python314.dll direct imports as reported by dumpbin /dependents in run 45707027584 (apiset contracts included). */
  try_system("imp_version", L"version.dll");
  try_system("imp_ws2_32", L"ws2_32.dll");
  try_system("imp_api-ms-win-core-path-l1-1-0", L"api-ms-win-core-path-l1-1-0.dll");
  try_system("imp_bcrypt", L"bcrypt.dll");
  try_system("imp_advapi32", L"advapi32.dll");
  try_system("imp_kernel32", L"kernel32.dll");
  try_system("imp_api-ms-win-crt-stdio-l1-1-0", L"api-ms-win-crt-stdio-l1-1-0.dll");
  try_system("imp_api-ms-win-crt-environment-l1-1-0", L"api-ms-win-crt-environment-l1-1-0.dll");
  try_system("imp_api-ms-win-crt-string-l1-1-0", L"api-ms-win-crt-string-l1-1-0.dll");
  try_system("imp_api-ms-win-crt-runtime-l1-1-0", L"api-ms-win-crt-runtime-l1-1-0.dll");
  try_system("imp_api-ms-win-crt-heap-l1-1-0", L"api-ms-win-crt-heap-l1-1-0.dll");
  try_system("imp_api-ms-win-crt-locale-l1-1-0", L"api-ms-win-crt-locale-l1-1-0.dll");
  try_system("imp_api-ms-win-crt-convert-l1-1-0", L"api-ms-win-crt-convert-l1-1-0.dll");
  try_system("imp_api-ms-win-crt-math-l1-1-0", L"api-ms-win-crt-math-l1-1-0.dll");
  try_system("imp_api-ms-win-crt-time-l1-1-0", L"api-ms-win-crt-time-l1-1-0.dll");
  try_system("imp_api-ms-win-crt-process-l1-1-0", L"api-ms-win-crt-process-l1-1-0.dll");
  try_system("imp_api-ms-win-crt-conio-l1-1-0", L"api-ms-win-crt-conio-l1-1-0.dll");
  try_system("imp_api-ms-win-crt-filesystem-l1-1-0", L"api-ms-win-crt-filesystem-l1-1-0.dll");
  /* Staged dependencies, then the interpreter DLL last. */
  try_staged("vcruntime140", self, L"vcruntime140.dll", LOAD_WITH_ALTERED_SEARCH_PATH, 1);
  try_staged("vcruntime140_1", self, L"vcruntime140_1.dll", LOAD_WITH_ALTERED_SEARCH_PATH, 1);
  /* Control: a trivial unsigned staged DLL under identical hardening flags. */
  try_staged("control_dll", self, L"probe_ctl.dll", LOAD_WITH_ALTERED_SEARCH_PATH, 0);
  /* Control: same trivial DLL plus one implicit static TLS variable; relevance to python314 is decided by the dumpbin /tls readout. */
  try_staged("control_tls_dll", self, L"probe_tls_ctl.dll", LOAD_WITH_ALTERED_SEARCH_PATH, 0);
  /* Control: probe_ctl.c source built without /guard:cf, so only the CFG image flag differs from control_dll. */
  try_staged("control_nocfg_dll", self, L"probe_nocfg_ctl.dll", LOAD_WITH_ALTERED_SEARCH_PATH, 0);
  try_staged("python314", self, L"python314.dll", LOAD_WITH_ALTERED_SEARCH_PATH, 0);
  /* Same file, split by stage: map without DllMain/import resolution, then map as image resource. */
  try_staged("python314_noresolve", self, L"python314.dll", DONT_RESOLVE_DLL_REFERENCES, 0);
  try_staged("python314_imageres", self, L"python314.dll", LOAD_LIBRARY_AS_IMAGE_RESOURCE, 0);
  emit("probe_done ok=1\n", 16);
  return 0;
}
