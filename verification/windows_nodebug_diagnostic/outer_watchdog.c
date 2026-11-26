/* DIAGNOSTIC ONLY. Outer whole-tree watchdog for one synthetic diagnostic.
 * Requires independent MSVC compilation and Windows qualification. Not a production launcher or acceptance path.
 * stdin/stdout/stderr are caller-owned regular temporary capture files.
 * Do not pass console/pipe handles: file size polling is a SOFT disk threshold.
 * The outer job adds KILL_ON_JOB_CLOSE only, no resource or breakaway limits.
 */
#define _WIN32_WINNT 0x0A00
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <wchar.h>
#include <string.h>
#ifndef PROC_THREAD_ATTRIBUTE_JOB_LIST
#define PROC_THREAD_ATTRIBUTE_JOB_LIST ProcThreadAttributeValue(ProcThreadAttributeJobList,FALSE,TRUE,FALSE)
#endif
static int empty_job(HANDLE job) {
 JOBOBJECT_BASIC_ACCOUNTING_INFORMATION accounting={0};
 if(!QueryInformationJobObject(job,JobObjectBasicAccountingInformation,&accounting,sizeof(accounting),NULL))return 0;
 return accounting.ActiveProcesses==0;
}
static int file_budget(HANDLE file,LONGLONG cap) {
 LARGE_INTEGER size;
 return GetFileType(file)==FILE_TYPE_DISK && GetFileSizeEx(file,&size) && size.QuadPart<=cap;
}
int wmain(int argc,wchar_t **argv) {
 HANDLE job=NULL,handles[3]={NULL,NULL,NULL};PROCESS_INFORMATION pi={0};STARTUPINFOEXW si={0};
 JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits={0};SIZE_T bytes=0;
 wchar_t command[32768];HANDLE originals[3]={NULL,NULL,NULL};
 int result=126,created=0,attrs_ready=0,clean=1,straggler=-1;const char *status="unavailable";
 ULONGLONG deadline=GetTickCount64()+15000;DWORD code=126;BOOL in_job=FALSE;
 /* Exact 5 arguments. Paths may contain spaces but never quotes. */
 if(argc!=6)goto done;
 for(int i=1;i<argc;i++){if(!argv[i][0]||wcschr(argv[i],L'"')||argv[i][wcslen(argv[i])-1]==L'\\')goto done;}
 if(_snwprintf_s(command,32768,_TRUNCATE,L"\"%ls\" \"%ls\" \"%ls\" \"%ls\" \"%ls\"",argv[1],argv[2],argv[3],argv[4],argv[5])<0)goto done;
 originals[0]=GetStdHandle(STD_INPUT_HANDLE);originals[1]=GetStdHandle(STD_OUTPUT_HANDLE);originals[2]=GetStdHandle(STD_ERROR_HANDLE);
 if(!file_budget(originals[0],16000000)||!file_budget(originals[1],4000000)||!file_budget(originals[2],65536))goto done;
 for(int i=0;i<3;i++){
  if(!DuplicateHandle(GetCurrentProcess(),originals[i],GetCurrentProcess(),&handles[i],0,TRUE,DUPLICATE_SAME_ACCESS))goto done;
 }
 job=CreateJobObjectW(NULL,NULL);if(!job)goto done;
 limits.BasicLimitInformation.LimitFlags=JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
 if(!SetInformationJobObject(job,JobObjectExtendedLimitInformation,&limits,sizeof(limits)))goto done;
 /* Atomic assignment at creation. No AssignProcess fallback and no breakaway. */
 InitializeProcThreadAttributeList(NULL,2,0,&bytes);
 if(!bytes)goto done;
 si.lpAttributeList=(LPPROC_THREAD_ATTRIBUTE_LIST)HeapAlloc(GetProcessHeap(),0,bytes);if(!si.lpAttributeList)goto done;
 if(!InitializeProcThreadAttributeList(si.lpAttributeList,2,0,&bytes))goto done;attrs_ready=1;
 if(!UpdateProcThreadAttribute(si.lpAttributeList,0,PROC_THREAD_ATTRIBUTE_HANDLE_LIST,handles,sizeof(handles),NULL,NULL))goto done;
 if(!UpdateProcThreadAttribute(si.lpAttributeList,0,PROC_THREAD_ATTRIBUTE_JOB_LIST,&job,sizeof(job),NULL,NULL))goto done;
 si.StartupInfo.cb=sizeof(si);si.StartupInfo.dwFlags=STARTF_USESTDHANDLES;
 si.StartupInfo.hStdInput=handles[0];si.StartupInfo.hStdOutput=handles[1];si.StartupInfo.hStdError=handles[2];
 /* Caller supplies the minimal environment to this trusted watchdog; inherited unchanged.
  * No outer job handle is inheritable, only the 3 explicit capture handles are. */
 if(GetTickCount64()>=deadline){status="timeout";goto done;}
 if(!CreateProcessW(argv[1],command,NULL,NULL,TRUE,CREATE_SUSPENDED|CREATE_NO_WINDOW|EXTENDED_STARTUPINFO_PRESENT,NULL,argv[4],&si.StartupInfo,&pi))goto done;
 created=1;
 if(!IsProcessInJob(pi.hProcess,job,&in_job)||!in_job)goto done;
 if(GetTickCount64()>=deadline){status="timeout";goto done;}
 if(ResumeThread(pi.hThread)!=1)goto done;
 for(;;){
  DWORD waited=WaitForSingleObject(pi.hProcess,20);
  if(waited==WAIT_OBJECT_0){
   if(!GetExitCodeProcess(pi.hProcess,&code))goto done;
   if(!file_budget(handles[1],4000000)||!file_budget(handles[2],65536)){status="output_refused";break;}
   status=code==0?"returned":"refused";result=code==0?0:126;break;
  }
  if(waited!=WAIT_TIMEOUT)goto done;
  if(!file_budget(handles[1],4000000)||!file_budget(handles[2],65536)){status="output_refused";break;}
  if(GetTickCount64()>=deadline){status="timeout";break;}
 }
 done:
 if(created){
  if(WaitForSingleObject(pi.hProcess,0)==WAIT_OBJECT_0)straggler=empty_job(job)?0:1;
  /* Always terminate the owned job, even after launcher exit, to remove stragglers.
   * Accounting empty is required in addition to waiting for the launcher. */
  if(!TerminateJobObject(job,126))clean=0;
  if(WaitForSingleObject(pi.hProcess,3000)!=WAIT_OBJECT_0)clean=0;
  {ULONGLONG until=GetTickCount64()+3000;while(!empty_job(job)&&GetTickCount64()<until)Sleep(20);if(!empty_job(job))clean=0;}
 }
 if(pi.hThread&&!CloseHandle(pi.hThread))clean=0;
 if(pi.hProcess&&!CloseHandle(pi.hProcess))clean=0;
 if(attrs_ready)DeleteProcThreadAttributeList(si.lpAttributeList);
 if(si.lpAttributeList&&!HeapFree(GetProcessHeap(),0,si.lpAttributeList))clean=0;
 for(int i=0;i<3;i++){if(handles[i]&&!CloseHandle(handles[i]))clean=0;}
 /* Close owned job last. Never close caller-owned capture handles. */
 if(job&&!CloseHandle(job))clean=0;
 if(!clean){status="cleanup_unconfirmed";result=125;}
 else if(strcmp(status,"timeout")==0)result=124;
 else if(strcmp(status,"output_refused")==0)result=123;
 else if(strcmp(status,"refused")==0)result=122;
 /* Fixed enum derives from trusted watchdog exit: 0 returned, 122 child refusal,
  * 123 output refusal, 124 timeout, 125 cleanup unconfirmed, 126 unavailable.
  * stderr detail remains captured and must never be treated as authority. */
 fprintf(stderr,"diag_nodebug_outer status=%s child_exit=%lu cleanup_confirmed=%d straggler_before_terminate=%d not_acceptance=1\n",status,(unsigned long)code,clean,straggler);
 return result;
}
