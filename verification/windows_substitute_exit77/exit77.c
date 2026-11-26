/* Diagnostic substitute only. Imports kernel32 only: ExitProcess(77).
 * No CRT, no file/network/registry/process access. Not Python, not a parser. */
#include <windows.h>

int mainCRTStartup(void)
{
    ExitProcess(77);
    return 77;
}
