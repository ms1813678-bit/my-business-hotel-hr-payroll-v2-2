/* Instinct synthetic-only Windows LPAC/JOB_LIST probe candidate.
 * Not integrated, not executed on Windows. No arbitrary executable or payload.
 * Requires an independently provisioned synthetic LPAC profile and file ACLs.
 * This program does NOT change ACLs, create profiles, firewall or exemptions.
 */
#define _WIN32_WINNT 0x0A00
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <windows.h>
#include <userenv.h>
#include <sddl.h>
#include <stdio.h>
#include <wchar.h>
#ifndef PROC_THREAD_ATTRIBUTE_JOB_LIST
#define PROC_THREAD_ATTRIBUTE_JOB_LIST ProcThreadAttributeValue(ProcThreadAttributeJobList,FALSE,TRUE,FALSE)
#endif
struct owners {
 HANDLE job,read_pipe,write_pipe,nul,process,thread;
 LPPROC_THREAD_ATTRIBUTE_LIST attrs;
 PSID sid;
 int attrs_initialized;
};
static void close_once(HANDLE *h,int *clean) {
 if(*h && *h!=INVALID_HANDLE_VALUE) {
  if(!CloseHandle(*h))*clean=0;
  /* Never reuse/retry ambiguous identity; process exits after cleanup. */
  *h=NULL;
 }
}
static int child_probe(const wchar_t *canary) {
 HANDLE token=NULL,f;DWORD is_ac=0,is_lpac=0,n=0;BOOL in_job=FALSE;
 JOBOBJECT_EXTENDED_LIMIT_INFORMATION lim={0};WSADATA wd;SOCKET s;int e;
 if(!OpenProcessToken(GetCurrentProcess(),TOKEN_QUERY,&token))return 31;
 if(!GetTokenInformation(token,TokenIsAppContainer,&is_ac,sizeof(is_ac),&n)||!is_ac){CloseHandle(token);return 32;}
 if(!GetTokenInformation(token,TokenIsLessPrivilegedAppContainer,&is_lpac,sizeof(is_lpac),&n)||!is_lpac){CloseHandle(token);return 41;}
 CloseHandle(token);
 if(!IsProcessInJob(GetCurrentProcess(),NULL,&in_job)||!in_job)return 33;
 if(!QueryInformationJobObject(NULL,JobObjectExtendedLimitInformation,&lim,sizeof(lim),NULL))return 34;
 if(lim.BasicLimitInformation.ActiveProcessLimit!=1 || lim.ProcessMemoryLimit!=512ULL*1024*1024 ||
 !(lim.BasicLimitInformation.LimitFlags&JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE))return 35;
 /* Canary must exist and be readable outside the LPAC before test launch.
    Only ERROR_ACCESS_DENIED counts; nonexistent file does not prove denial. */
 f=CreateFileW(canary,GENERIC_READ,FILE_SHARE_READ,NULL,OPEN_EXISTING,0,NULL);
 if(f!=INVALID_HANDLE_VALUE){CloseHandle(f);return 36;}
 if(GetLastError()!=ERROR_ACCESS_DENIED)return 37;
 if(WSAStartup(MAKEWORD(2,2),&wd))return 38;
 s=socket(AF_INET,SOCK_STREAM,IPPROTO_TCP);
 if(s==INVALID_SOCKET){e=WSAGetLastError();WSACleanup();if(e!=WSAEACCES)return 39;}
 else {
  struct sockaddr_in a={0};u_long nonblock=1;a.sin_family=AF_INET;a.sin_port=htons(9);a.sin_addr.s_addr=htonl(INADDR_LOOPBACK);
  ioctlsocket(s,FIONBIO,&nonblock);
  e=connect(s,(struct sockaddr*)&a,sizeof(a));e=(e==SOCKET_ERROR)?WSAGetLastError():0;
  closesocket(s);WSACleanup();if(e!=WSAEACCES)return 40;
 }
 puts("synthetic probe: AppContainer token, job limits, file access denied, loopback access denied");
 fflush(stdout);return 0;
}
static int verify_created_child(HANDLE process,HANDLE job,PSID expected_sid) {
 HANDLE token=NULL;DWORD size=0;PTOKEN_APPCONTAINER_INFORMATION info=NULL;
 BOOL bound=FALSE;int ok=0;DWORD lpac=0,returned=0,error=0;
 if(!IsProcessInJob(process,job,&bound)||!bound)return 0;
 if(!OpenProcessToken(process,TOKEN_QUERY,&token))return 0;
 if(!GetTokenInformation(token,TokenIsLessPrivilegedAppContainer,&lpac,sizeof(lpac),&returned)||!lpac)goto done;
 GetTokenInformation(token,TokenAppContainerSid,NULL,0,&size);
 if(!size)goto done;
 info=HeapAlloc(GetProcessHeap(),HEAP_ZERO_MEMORY,size);if(!info)goto done;
 if(!GetTokenInformation(token,TokenAppContainerSid,info,size,&size))goto done;
 if(!info->TokenAppContainer || !EqualSid(info->TokenAppContainer,expected_sid))goto done;
 ok=1;
 done:
 error=GetLastError();
 if(info)HeapFree(GetProcessHeap(),0,info);
 if(!CloseHandle(token))ok=0;
 if(!ok)SetLastError(error);
 return ok;
}
int wmain(int argc,wchar_t **argv) {
 struct owners o={0};SECURITY_ATTRIBUTES sa={sizeof(sa),NULL,TRUE};
 JOBOBJECT_EXTENDED_LIMIT_INFORMATION lim={0};SECURITY_CAPABILITIES cap={0};
 STARTUPINFOEXW si={0};PROCESS_INFORMATION pi={0};SIZE_T size=0;
 HANDLE inherited[2];DWORD optout=PROCESS_CREATION_ALL_APPLICATION_PACKAGES_OPT_OUT;
 wchar_t exe[32768],cmd[32768],systemroot[32768],env[32768];DWORD rootlen;DWORD wait,code=99,available=0,got=0;char output[1025]={0};
 int result=1,clean=1,created=0;DWORD failure_error=0;const char *stage="arguments";
 if(argc==3 && !wcscmp(argv[1],L"--probe-child"))return child_probe(argv[2]);
 if(argc!=3 || wcschr(argv[1],L'"') || wcschr(argv[2],L'"')) {
  fputs("Synthetic probe only: profile-name existing-synthetic-canary-path\n",stderr);return 2;
 }
 /* Verify the synthetic canary exists before launch. Never read or print it. */
 {HANDLE f=CreateFileW(argv[2],GENERIC_READ,FILE_SHARE_READ,NULL,OPEN_EXISTING,0,NULL);
  if(f==INVALID_HANDLE_VALUE)return 3;CloseHandle(f);}
 stage="derive_sid";SetLastError(ERROR_SUCCESS);
 if(FAILED(DeriveAppContainerSidFromAppContainerName(argv[1],&o.sid)))goto done;
 stage="create_job";SetLastError(ERROR_SUCCESS);
 o.job=CreateJobObjectW(NULL,NULL);if(!o.job)goto done;
 lim.BasicLimitInformation.LimitFlags=JOB_OBJECT_LIMIT_PROCESS_TIME|JOB_OBJECT_LIMIT_ACTIVE_PROCESS|
 JOB_OBJECT_LIMIT_PROCESS_MEMORY|JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE|JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION;
 lim.BasicLimitInformation.PerProcessUserTimeLimit.QuadPart=20000000;lim.BasicLimitInformation.ActiveProcessLimit=1;
 lim.ProcessMemoryLimit=512ULL*1024*1024;
 stage="configure_job";SetLastError(ERROR_SUCCESS);
 if(!SetInformationJobObject(o.job,JobObjectExtendedLimitInformation,&lim,sizeof(lim)))goto done;
 stage="create_pipe";SetLastError(ERROR_SUCCESS);
 if(!CreatePipe(&o.read_pipe,&o.write_pipe,&sa,0))goto done;
 stage="read_noinherit";SetLastError(ERROR_SUCCESS);
 if(!SetHandleInformation(o.read_pipe,HANDLE_FLAG_INHERIT,0))goto done;
 stage="open_nul";SetLastError(ERROR_SUCCESS);
 o.nul=CreateFileW(L"NUL",GENERIC_READ|GENERIC_WRITE,FILE_SHARE_READ|FILE_SHARE_WRITE,&sa,OPEN_EXISTING,0,NULL);
 if(o.nul==INVALID_HANDLE_VALUE)goto done;
 stage="attribute_size";SetLastError(ERROR_SUCCESS);
 InitializeProcThreadAttributeList(NULL,4,0,&size);if(!size)goto done;
 stage="attribute_allocate";SetLastError(ERROR_SUCCESS);
 o.attrs=HeapAlloc(GetProcessHeap(),HEAP_ZERO_MEMORY,size);if(!o.attrs)goto done;
 stage="attribute_init";SetLastError(ERROR_SUCCESS);
 if(!InitializeProcThreadAttributeList(o.attrs,4,0,&size))goto done;o.attrs_initialized=1;
 inherited[0]=o.nul;inherited[1]=o.write_pipe;
 cap.AppContainerSid=o.sid;cap.Capabilities=NULL;cap.CapabilityCount=0;
 stage="attribute_handles";SetLastError(ERROR_SUCCESS);
 if(!UpdateProcThreadAttribute(o.attrs,0,PROC_THREAD_ATTRIBUTE_HANDLE_LIST,inherited,sizeof(inherited),NULL,NULL))goto done;
 stage="attribute_job";SetLastError(ERROR_SUCCESS);
 if(!UpdateProcThreadAttribute(o.attrs,0,PROC_THREAD_ATTRIBUTE_JOB_LIST,&o.job,sizeof(o.job),NULL,NULL))goto done;
 stage="attribute_capabilities";SetLastError(ERROR_SUCCESS);
 if(!UpdateProcThreadAttribute(o.attrs,0,PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,&cap,sizeof(cap),NULL,NULL))goto done;
 stage="attribute_lpac";SetLastError(ERROR_SUCCESS);
 if(!UpdateProcThreadAttribute(o.attrs,0,PROC_THREAD_ATTRIBUTE_ALL_APPLICATION_PACKAGES_POLICY,&optout,sizeof(optout),NULL,NULL))goto done;
 stage="systemroot";SetLastError(ERROR_SUCCESS);
 rootlen=GetEnvironmentVariableW(L"SYSTEMROOT",systemroot,32768);
 if(!rootlen || rootlen>=32768)goto done;
 stage="environment_format";SetLastError(ERROR_SUCCESS);
 if(_snwprintf(env,32767,L"SYSTEMROOT=%ls",systemroot)<0)goto done;
 env[wcslen(env)+1]=L'\0'; /* Explicit double NUL; no inherited secrets. */
 stage="self_exe";SetLastError(ERROR_SUCCESS);
 if(!GetModuleFileNameW(NULL,exe,32768))goto done;
 stage="command_format";SetLastError(ERROR_SUCCESS);
 if(_snwprintf(cmd,32768,L"\"%ls\" --probe-child \"%ls\"",exe,argv[2])<0)goto done;
 si.StartupInfo.cb=sizeof(si);si.lpAttributeList=o.attrs;si.StartupInfo.dwFlags=STARTF_USESTDHANDLES;
 si.StartupInfo.hStdInput=o.nul;si.StartupInfo.hStdOutput=o.write_pipe;si.StartupInfo.hStdError=o.nul;
 /* Output pair is written directly into the stack-owned pi structure by OS.
    No Python callback/adoption exists. Caller remains single native process. */
 stage="create_suspended_child";SetLastError(ERROR_SUCCESS);
 if(!CreateProcessW(exe,cmd,NULL,NULL,TRUE,CREATE_SUSPENDED|CREATE_NO_WINDOW|CREATE_UNICODE_ENVIRONMENT|EXTENDED_STARTUPINFO_PRESENT,
 env,NULL,&si.StartupInfo,&pi))goto done;
 created=1;o.process=pi.hProcess;o.thread=pi.hThread;
 stage="verify_child";SetLastError(ERROR_SUCCESS);
 if(!verify_created_child(o.process,o.job,o.sid))goto done;
 stage="close_parent_writer";SetLastError(ERROR_SUCCESS);
 close_once(&o.write_pipe,&clean);if(!clean)goto done;
 stage="resume";SetLastError(ERROR_SUCCESS);
 if(ResumeThread(o.thread)!=1)goto done;
 stage="work_wait";SetLastError(ERROR_SUCCESS);
 wait=WaitForSingleObject(o.process,3000);if(wait!=WAIT_OBJECT_0)goto done;
 stage="child_exit";SetLastError(ERROR_SUCCESS);
 if(!GetExitCodeProcess(o.process,&code))goto done;
 stage="bounded_output";SetLastError(ERROR_SUCCESS);
 if(!PeekNamedPipe(o.read_pipe,NULL,0,NULL,&available,NULL)||available>1024)goto done;
 stage="read_output";SetLastError(ERROR_SUCCESS);
 if(available && (!ReadFile(o.read_pipe,output,available,&got,NULL)||got!=available))goto done;
 if(code==0){fwrite(output,1,got,stdout);result=0;}
 else fprintf(stderr,"Synthetic child refused/inconclusive: %lu\n",(unsigned long)code);
 done:
 failure_error=GetLastError();
 if(result!=0)fprintf(stderr,"Synthetic probe refusal: stage %s; Win32 error %lu; child code %lu\n",stage,(unsigned long)failure_error,(unsigned long)code);
 if(created){
  if(!TerminateJobObject(o.job,99))clean=0;
  if(WaitForSingleObject(o.process,2000)!=WAIT_OBJECT_0)clean=0;
 }
 close_once(&o.thread,&clean);close_once(&o.process,&clean);
 close_once(&o.write_pipe,&clean);close_once(&o.read_pipe,&clean);close_once(&o.nul,&clean);close_once(&o.job,&clean);
 if(o.attrs_initialized)DeleteProcThreadAttributeList(o.attrs);
 if(o.attrs)HeapFree(GetProcessHeap(),0,o.attrs);if(o.sid)FreeSid(o.sid);
 if(!clean)result=1;
 return result;
}
