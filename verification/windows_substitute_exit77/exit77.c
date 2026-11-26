/* Diagnostic substitute only. Imports kernel32 only: ExitProcess(77).
 * No CRT, no file/network/registry/process access. Not Python, not a parser.
 * void entry: ExitProcess is declared no-return, so no statement follows it. */
#include <windows.h>

void mainCRTStartup(void)
{
    ExitProcess(77);
}
