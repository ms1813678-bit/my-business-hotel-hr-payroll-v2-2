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

static void report(const char *label, HMODULE module, DWORD error) {
  char line[96];
  DWORD at = put(line, 0, "probe_load label=");
  at = put(line, at, label);
  at = put(line, at, module ? " ok=1 error=0" : " ok=0 error=");
  if (!module) at = put_number(line, at, error);
  line[at++] = '\n';
  emit(line, at);
}

static void try_system(const char *label, const wchar_t *name) {
  SetLastError(0);
  HMODULE module = LoadLibraryExW(name, NULL, LOAD_LIBRARY_SEARCH_SYSTEM32);
  DWORD error = module ? 0 : GetLastError();
  report(label, module, error);
  if (module) (void)FreeLibrary(module);
}

static void try_staged(const char *label, const wchar_t *directory, const wchar_t *name, int keep) {
  wchar_t path[MAX_PATH + 32];
  size_t used = 0;
  while (directory[used] && used < MAX_PATH) {
    path[used] = directory[used];
    used++;
  }
  for (size_t i = 0; name[i] && used < MAX_PATH + 30; i++) path[used++] = name[i];
  path[used] = 0;
  SetLastError(0);
  HMODULE module = LoadLibraryExW(path, NULL, LOAD_WITH_ALTERED_SEARCH_PATH);
  DWORD error = module ? 0 : GetLastError();
  report(label, module, error);
  if (module && !keep) (void)FreeLibrary(module);
}

int wmain(void) {
  wchar_t self[MAX_PATH + 1] = {0};
  DWORD length = GetModuleFileNameW(NULL, self, MAX_PATH);
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
  /* Staged dependencies, then the interpreter DLL last. */
  try_staged("vcruntime140", self, L"vcruntime140.dll", 1);
  try_staged("vcruntime140_1", self, L"vcruntime140_1.dll", 1);
  try_staged("python314", self, L"python314.dll", 0);
  emit("probe_done ok=1\n", 16);
  return 0;
}
