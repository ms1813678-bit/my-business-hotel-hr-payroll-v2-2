/* Diagnostic only: trivial unsigned control DLL with one implicit static TLS variable.
 * Built with the same flags as probe_ctl.dll so that TLS is the only difference. */
#include <windows.h>

__declspec(thread) static volatile LONG probe_tls_value = 1;

BOOL WINAPI DllMain(HINSTANCE module, DWORD reason, LPVOID reserved) {
  (void)module;
  (void)reserved;
  if (reason == DLL_PROCESS_ATTACH) probe_tls_value = 2;
  return TRUE;
}
