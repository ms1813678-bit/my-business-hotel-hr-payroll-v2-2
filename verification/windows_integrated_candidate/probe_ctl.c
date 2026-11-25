/* Diagnostic only: trivial unsigned control DLL, built with the same hardening flags as the probe.
 * It has no exports of interest, no imports beyond kernel32 and does nothing in DllMain. */
#include <windows.h>

BOOL WINAPI DllMain(HINSTANCE module, DWORD reason, LPVOID reserved) {
  (void)module;
  (void)reason;
  (void)reserved;
  return TRUE;
}
