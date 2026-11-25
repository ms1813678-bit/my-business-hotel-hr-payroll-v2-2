/* LOCAL CANDIDATE ONLY. Not production, not executed on Windows.
 * Caller owns disposable profile and explicit RX-only runtime provisioning.
 * Does not create profiles, change ACLs, capabilities or firewall policy.
 */
#define _WIN32_WINNT 0x0A00
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <userenv.h>
#include <sddl.h>
#include <stdio.h>
#include <stdint.h>
#include <io.h>
#include <fcntl.h>
#include <wchar.h>
#include "token_checks.h"
#include "startup_diagnostics.h"
#include "real_open_diagnostics.h"
#include <string.h>
#include "loader_debug.h"
#ifndef PROC_THREAD_ATTRIBUTE_JOB_LIST
#define PROC_THREAD_ATTRIBUTE_JOB_LIST ProcThreadAttributeValue(ProcThreadAttributeJobList,FALSE,TRUE,FALSE)
#endif
struct drain { HANDLE pipe; BYTE *bytes; DWORD size; int ok; };
static DWORD WINAPI drain_output(LPVOID context){
 struct drain *d=context;
 for(;;){DWORD got=0,room=4000001-d->size;if(!room)return 1;
  if(!ReadFile(d->pipe,d->bytes+d->size,room,&got,NULL)){if(GetLastError()==ERROR_BROKEN_PIPE){d->ok=1;return 0;}return 1;}
  if(!got)return 1;d->size+=got;if(d->size>4000000)return 1;
 }
}
struct feed { HANDLE pipe; const BYTE *bytes; DWORD size; int ok; };
static DWORD WINAPI feed_input(LPVOID context) {
 struct feed *f=context;DWORD position=0;
 while(position<f->size){DWORD wrote=0;if(!WriteFile(f->pipe,f->bytes+position,f->size-position,&wrote,NULL)||!wrote)return 1;position+=wrote;}
 f->ok=1;return 0;
}
static void close_owned(HANDLE *h,int *clean){if(*h && *h!=INVALID_HANDLE_VALUE){if(!CloseHandle(*h))*clean=0;*h=NULL;}}
int wmain(int argc,wchar_t **argv){
 HANDLE image=NULL,reader=NULL,input=NULL,job=NULL,in_read=NULL,in_write=NULL,out_read=NULL,out_write=NULL,nul=NULL,feeder=NULL;
 HANDLE inherited[3];PROCESS_INFORMATION pi={0};STARTUPINFOEXW si={0};SECURITY_ATTRIBUTES sa={sizeof(sa),NULL,TRUE};
 JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits={0};SECURITY_CAPABILITIES caps={0};PSID sid=NULL;
 SIZE_T attr_size=0;LPPROC_THREAD_ATTRIBUTE_LIST attrs=NULL;int attrs_ready=0,clean=1,result=1,created=0;
 BYTE *request=NULL,*response=NULL;DWORD request_size=0,response_size=0,code=99;struct feed feed={0};struct drain drain={0};
 wchar_t command[32768],environment[65536],systemroot[32000],localappdata[32000],actual_image[32768];DWORD optout=PROCESS_CREATION_ALL_APPLICATION_PACKAGES_OPT_OUT;
 struct loader_debug_state debug={0};DEBUG_EVENT held={0};int event_held=0;
 const char *stage="arguments";
 /* profile, canonical python.exe, runtime cwd, candidate worker. Request on stdin. */
 if(argc!=5)return 2;
 if(_setmode(_fileno(stdout),_O_BINARY)==-1)return 2;
 for(int i=1;i<argc;i++)if(wcschr(argv[i],L'"'))return 2;
 if(wcslen(argv[2])<4 || argv[2][1]!=L':' || argv[2][2]!=L'\\' || wcsstr(argv[2],L".."))return 2;
 stage="hold_image";image=CreateFileW(argv[2],GENERIC_READ,FILE_SHARE_READ,NULL,OPEN_EXISTING,FILE_FLAG_OPEN_REPARSE_POINT,NULL);
 if(image==INVALID_HANDLE_VALUE)goto done;
 {BY_HANDLE_FILE_INFORMATION identity={0};if(!GetFileInformationByHandle(image,&identity)||(identity.dwFileAttributes&FILE_ATTRIBUTE_REPARSE_POINT))goto done;}
 {DWORD n=GetFinalPathNameByHandleW(image,actual_image,32768,FILE_NAME_NORMALIZED|VOLUME_NAME_DOS);
 if(!n||n>=32768||wcsncmp(actual_image,L"\\\\?\\",4)||_wcsicmp(actual_image+4,argv[2]))goto done;}
 stage="read_input";input=GetStdHandle(STD_INPUT_HANDLE);
 if(!input||input==INVALID_HANDLE_VALUE)goto done;
 request=HeapAlloc(GetProcessHeap(),0,16000001);response=HeapAlloc(GetProcessHeap(),0,4000001);
 if(!request||!response)goto done;
 for(;;){DWORD got=0;if(request_size>=16000001)goto done;
 if(!ReadFile(input,request+request_size,16000001-request_size,&got,NULL)){if(GetLastError()==ERROR_BROKEN_PIPE)break;goto done;}
 if(!got)break;request_size+=got;}
 if(!request_size||request_size>16000000)goto done;
 input=NULL; /* borrowed standard handle, never closed as an allocated identity */
 stage="derive_profile";if(FAILED(DeriveAppContainerSidFromAppContainerName(argv[1],&sid)))goto done;
 stage="job";job=CreateJobObjectW(NULL,NULL);if(!job)goto done;
 limits.BasicLimitInformation.LimitFlags=JOB_OBJECT_LIMIT_PROCESS_TIME|JOB_OBJECT_LIMIT_ACTIVE_PROCESS|JOB_OBJECT_LIMIT_PROCESS_MEMORY|JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE|JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION;
 limits.BasicLimitInformation.PerProcessUserTimeLimit.QuadPart=20000000;limits.BasicLimitInformation.ActiveProcessLimit=1;limits.ProcessMemoryLimit=512ULL*1024*1024;
 if(!SetInformationJobObject(job,JobObjectExtendedLimitInformation,&limits,sizeof(limits)))goto done;
 stage="pipes";if(!CreatePipe(&in_read,&in_write,&sa,1048576)||!CreatePipe(&out_read,&out_write,&sa,1048576))goto done;
 if(!SetHandleInformation(in_write,HANDLE_FLAG_INHERIT,0)||!SetHandleInformation(out_read,HANDLE_FLAG_INHERIT,0))goto done;
 nul=CreateFileW(L"NUL",GENERIC_WRITE,FILE_SHARE_READ|FILE_SHARE_WRITE,&sa,OPEN_EXISTING,0,NULL);if(nul==INVALID_HANDLE_VALUE)goto done;
 stage="attributes";InitializeProcThreadAttributeList(NULL,4,0,&attr_size);if(!attr_size)goto done;
 attrs=HeapAlloc(GetProcessHeap(),HEAP_ZERO_MEMORY,attr_size);if(!attrs||!InitializeProcThreadAttributeList(attrs,4,0,&attr_size))goto done;attrs_ready=1;
 inherited[0]=in_read;inherited[1]=out_write;inherited[2]=nul;caps.AppContainerSid=sid;
 if(!UpdateProcThreadAttribute(attrs,0,PROC_THREAD_ATTRIBUTE_HANDLE_LIST,inherited,sizeof(inherited),NULL,NULL)||
 !UpdateProcThreadAttribute(attrs,0,PROC_THREAD_ATTRIBUTE_JOB_LIST,&job,sizeof(job),NULL,NULL)||
 !UpdateProcThreadAttribute(attrs,0,PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,&caps,sizeof(caps),NULL,NULL)||
 !UpdateProcThreadAttribute(attrs,0,PROC_THREAD_ATTRIBUTE_ALL_APPLICATION_PACKAGES_POLICY,&optout,sizeof(optout),NULL,NULL))goto done;
 stage="environment";{DWORD n=GetEnvironmentVariableW(L"SYSTEMROOT",systemroot,32000);if(!n||n>=32000)goto done;
 n=GetEnvironmentVariableW(L"LOCALAPPDATA",localappdata,32000);if(!n||n>=32000)goto done;}
 if(_snwprintf_s(environment,65534,_TRUNCATE,L"LOCALAPPDATA=%ls",localappdata)<0)goto done;
 {size_t offset=wcslen(environment)+1;if(_snwprintf_s(environment+offset,65534-offset,_TRUNCATE,L"SYSTEMROOT=%ls",systemroot)<0)goto done;environment[offset+wcslen(environment+offset)+1]=0;}
 if(_snwprintf_s(command,32768,_TRUNCATE,L"\"%ls\" -I -B -X utf8 \"%ls\"",argv[2],argv[4])<0)goto done;
 si.StartupInfo.cb=sizeof(si);si.StartupInfo.dwFlags=STARTF_USESTDHANDLES;si.StartupInfo.hStdInput=in_read;si.StartupInfo.hStdOutput=out_write;si.StartupInfo.hStdError=nul;si.lpAttributeList=attrs;
 stage="create_suspended";if(!CreateProcessW(argv[2],command,NULL,NULL,TRUE,DEBUG_ONLY_THIS_PROCESS|CREATE_SUSPENDED|CREATE_NO_WINDOW|CREATE_UNICODE_ENVIRONMENT|EXTENDED_STARTUPINFO_PRESENT,environment,argv[3],&si.StartupInfo,&pi))goto done;created=1;debug.attached=1;
 {DWORD n=32768;if(!QueryFullProcessImageNameW(pi.hProcess,0,actual_image,&n)||_wcsicmp(actual_image,argv[2]))goto done;}
 fprintf(stderr,"CANDIDATE_CREATED pid=%lu suspended=1\n",(unsigned long)GetProcessId(pi.hProcess));
 stage="verify_before_resume";if(!verify_created_child(pi.hProcess,job,sid,1))goto done;
 /* Debug relation already established atomically at creation. */
 stage="resume_before_first_creation_event";
 {SetLastError(0);DWORD prior=ResumeThread(pi.hThread);DWORD error=prior==(DWORD)-1?GetLastError():0;
 fprintf(stderr,"loader_debug_resume prior_suspend_count=%lu api_failed=%d api_error=%lu pre_resume_token_job_verified=1 distinct_creation_baseline=1\n",(unsigned long)prior,prior==(DWORD)-1,(unsigned long)error);
 if(prior!=1)goto done;}
 stage="hold_initial_debug_event";
 if(!WaitForDebugEventEx(&held,2000))goto done;event_held=1;
 if(held.dwDebugEventCode!=CREATE_PROCESS_DEBUG_EVENT || held.dwProcessId!=pi.dwProcessId || held.dwThreadId!=pi.dwThreadId)goto done;
 {DWORD n=32768;if(!QueryFullProcessImageNameW(held.u.CreateProcessInfo.hProcess,0,actual_image,&n)||_wcsicmp(actual_image,argv[2]))goto done;}
 stage="verify_after_attach_held_event";if(!verify_created_child(pi.hProcess,job,sid,1))goto done;
 fputs("loader_debug_held initial_create_event=1 exact_pid_thread_image=1 token_job_reverified=1\n",stderr);
 fputs("loader_debug_created distinct_creation_baseline=1 token_job_reverified=1 debugger_observed_only=1\n",stderr);
 /* Advisory function return is cleanup hygiene, NOT an access verdict. */
 stage="startup_readonly_diagnostics";if(!diagnose_startup(pi.hProcess,argv[3]))goto done;
 stage="startup_fixed_real_open";if(!diagnose_real_open(pi.hProcess,argv[3]))goto done;
 close_owned(&in_read,&clean);close_owned(&out_write,&clean);if(!clean)goto done;
 feed.pipe=in_write;feed.bytes=request;feed.size=request_size;
 stage="start_feeder";feeder=CreateThread(NULL,0,feed_input,&feed,0,NULL);if(!feeder)goto done;
 drain.pipe=out_read;drain.bytes=response;
 stage="start_reader";reader=CreateThread(NULL,0,drain_output,&drain,0,NULL);if(!reader)goto done;
 if(held.u.CreateProcessInfo.hFile){HANDLE owned_file=held.u.CreateProcessInfo.hFile;held.u.CreateProcessInfo.hFile=NULL;if(!CloseHandle(owned_file))goto done;}
 stage="continue_initial_debug_event";if(!ContinueDebugEvent(held.dwProcessId,held.dwThreadId,DBG_CONTINUE))goto done;event_held=0;
 fputs("CANDIDATE_RESUMED containment_verified=1\n",stderr);
 stage="duplex_wait";{ULONGLONG deadline=GetTickCount64()+3000;int fed=0,read_done=0,exited=0;
 while(!fed||!read_done||!exited||!debug.exited){
 for(unsigned int burst=0;burst<64;burst++){int polled=loader_debug_poll(&debug,pi.hProcess,pi.dwProcessId);if(!polled)goto done;if(polled==2||debug.exited)break;}HANDLE waits[3];DWORD count=0;int ids[3];ULONGLONG now=GetTickCount64();
 if(now>=deadline){SetLastError(ERROR_TIMEOUT);goto done;}
 if(!fed){waits[count]=feeder;ids[count++]=0;}if(!read_done){waits[count]=reader;ids[count++]=1;}if(!exited){waits[count]=pi.hProcess;ids[count++]=2;}
 if(!count){Sleep(1);continue;}
 DWORD w=WaitForMultipleObjects(count,waits,FALSE,10);if(w==WAIT_TIMEOUT)continue;if(w>=WAIT_OBJECT_0+count)goto done;
 switch(ids[w-WAIT_OBJECT_0]){
 case 0:if(!feed.ok)goto done;fed=1;close_owned(&in_write,&clean);if(!clean)goto done;break;
 case 1:if(!drain.ok)goto done;read_done=1;break;
 case 2:if(!GetExitCodeProcess(pi.hProcess,&code)||code!=0)goto done;exited=1;break;
 }
 }
 response_size=drain.size;
 }
 result=0;
 done:
 if(created){DWORD preserved=GetLastError();diagnose_exit(pi.hProcess);SetLastError(preserved);}
 if(result)fprintf(stderr,"CANDIDATE_REFUSAL stage=%s error=%lu child=%lu\n",stage,(unsigned long)GetLastError(),(unsigned long)code);
 if(created){if(!TerminateJobObject(job,99))clean=0;
 if(event_held){
  if(held.dwDebugEventCode==CREATE_PROCESS_DEBUG_EVENT && held.u.CreateProcessInfo.hFile){if(!CloseHandle(held.u.CreateProcessInfo.hFile))clean=0;}
  if(held.dwDebugEventCode==LOAD_DLL_DEBUG_EVENT && held.u.LoadDll.hFile){if(!CloseHandle(held.u.LoadDll.hFile))clean=0;}
  if(!ContinueDebugEvent(held.dwProcessId,held.dwThreadId,held.dwDebugEventCode==EXCEPTION_DEBUG_EVENT?DBG_EXCEPTION_NOT_HANDLED:DBG_CONTINUE)){fputs("HELD_DEBUG_EVENT_CLEANUP_UNCONFIRMED no_detach=1 fail_closed_exit=128\n",stderr);ExitProcess(128);}event_held=0;
 }

 if(debug.attached&&!debug.exited){ULONGLONG until=GetTickCount64()+2000;while(!debug.exited&&GetTickCount64()<until){if(!loader_debug_poll(&debug,pi.hProcess,pi.dwProcessId)){clean=0;break;}Sleep(1);}if(!debug.exited){fputs("DEBUG_CLEANUP_UNCONFIRMED no_detach=1 fail_closed_exit=128\n",stderr);ExitProcess(128);}}
if(WaitForSingleObject(pi.hProcess,2000)!=WAIT_OBJECT_0)clean=0;}
 if(reader){DWORD waited=WaitForSingleObject(reader,0);if(waited!=WAIT_OBJECT_0){if(!CancelSynchronousIo(reader)&&GetLastError()!=ERROR_NOT_FOUND)clean=0;waited=WaitForSingleObject(reader,2000);}if(waited!=WAIT_OBJECT_0){fprintf(stderr,"READER_CLEANUP_UNCONFIRMED\n");ExitProcess(125);}}
 if(feeder){DWORD waited=WaitForSingleObject(feeder,0);if(waited!=WAIT_OBJECT_0){if(!CancelSynchronousIo(feeder)&&GetLastError()!=ERROR_NOT_FOUND)clean=0;waited=WaitForSingleObject(feeder,2000);}if(waited!=WAIT_OBJECT_0){fprintf(stderr,"FEEDER_CLEANUP_UNCONFIRMED\n");ExitProcess(125);}}
 close_owned(&reader,&clean);close_owned(&image,&clean);close_owned(&feeder,&clean);close_owned(&in_write,&clean);close_owned(&in_read,&clean);close_owned(&out_read,&clean);close_owned(&out_write,&clean);close_owned(&nul,&clean);
 close_owned(&pi.hThread,&clean);close_owned(&pi.hProcess,&clean);close_owned(&job,&clean);
 if(attrs_ready)DeleteProcThreadAttributeList(attrs);if(attrs&&!HeapFree(GetProcessHeap(),0,attrs))clean=0;if(sid&&FreeSid(sid))clean=0;
 if(request&&!HeapFree(GetProcessHeap(),0,request))clean=0;
 if(!clean)result=1;
 if(!result){if(fwrite(response,1,response_size,stdout)!=response_size||fflush(stdout))result=1;}
 if(response&&!HeapFree(GetProcessHeap(),0,response))result=1;
 fprintf(stderr,"CANDIDATE_CLEANUP complete=%d workload=%s security_acceptance=NOT_ESTABLISHED\n",clean,result?"refused":"returned");return result;
}
