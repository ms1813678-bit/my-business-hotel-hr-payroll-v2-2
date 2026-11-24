/* Synthetic-only suspended Windows policy diagnostic candidate.
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
#include <stdint.h>
#include <stddef.h>
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
struct observation { BOOL api_ok,allowed;DWORD error,granted; };
static struct observation check(HANDLE token,const wchar_t *sddl) {
 struct observation r={0};PSECURITY_DESCRIPTOR sd=NULL;
 PRIVILEGE_SET initial={0};DWORD needed=sizeof(initial),desired=GENERIC_READ;
 PPRIVILEGE_SET privileges=&initial;GENERIC_MAPPING mapping={FILE_GENERIC_READ,FILE_GENERIC_WRITE,FILE_GENERIC_EXECUTE,FILE_ALL_ACCESS};
 MapGenericMask(&desired,&mapping);
 if(!ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl,SDDL_REVISION_1,&sd,NULL)){r.error=GetLastError();return r;}
 SetLastError(0);r.api_ok=AccessCheck(sd,token,desired,&mapping,privileges,&needed,&r.granted,&r.allowed);r.error=GetLastError();
 if(!r.api_ok && r.error==ERROR_INSUFFICIENT_BUFFER && needed>sizeof(initial) && needed<=65536){
  privileges=HeapAlloc(GetProcessHeap(),HEAP_ZERO_MEMORY,needed);
  if(!privileges){r.error=ERROR_NOT_ENOUGH_MEMORY;goto done;}
  SetLastError(0);r.api_ok=AccessCheck(sd,token,desired,&mapping,privileges,&needed,&r.granted,&r.allowed);r.error=GetLastError();
 }
 done:
 if(privileges && privileges!=&initial && !HeapFree(GetProcessHeap(),0,privileges)){r.api_ok=FALSE;r.error=GetLastError();}
 if(LocalFree(sd)){r.api_ok=FALSE;r.error=GetLastError();}
 return r;
}
/* caller must independently verify exact process/job/packageSID/zero capabilities.
 * Required controls: actual LPAC candidate and separately created ordinary AC,
 * identical user, job, profile ACLs; expected matrix checked by harness.
 * This never resumes any process and never opens the named synthetic resources. */
int inspect_policy(HANDLE primary_token,PSID package_sid,const char *label,int is_lpac) {
 HANDLE duplicate=NULL;LPWSTR text_sid=NULL;wchar_t profile[512];
 const wchar_t *descriptors[3];const char *names[]={"profile_sid","all_application_packages","everyone_only"};int complete=1;
 SetLastError(0);BOOL duplicated=DuplicateToken(primary_token,SecurityImpersonation,&duplicate);DWORD duplicate_error=GetLastError();
 fprintf(stderr,"policy duplicate: label=%s api=%d error=%lu\n",label,duplicated,(unsigned long)duplicate_error);
 if(!duplicated)return 0;
 if(!ConvertSidToStringSidW(package_sid,&text_sid)){complete=0;goto done;}
 if(_snwprintf(profile,512,L"O:SYG:SYD:(A;;FR;;;WD)(A;;FR;;;%ls)",text_sid)<0){complete=0;goto done;}
 descriptors[0]=profile;
 descriptors[1]=L"O:SYG:SYD:(A;;FR;;;WD)(A;;FR;;;S-1-15-2-1)";
 descriptors[2]=L"O:SYG:SYD:(A;;FR;;;WD)";
 for(unsigned int i=0;i<3;i++){
  struct observation r=check(duplicate,descriptors[i]);
  fprintf(stderr,"policy_observation label=%s descriptor=%s api=%d allowed=%d granted=%lu error=%lu\n",label,names[i],r.api_ok,r.allowed,(unsigned long)r.granted,(unsigned long)r.error);
  if(!r.api_ok || r.allowed!=(i==0 || (i==1 && !is_lpac)) || (r.allowed && r.granted!=FILE_GENERIC_READ) || (!r.allowed && r.granted!=0))complete=0;
 }
 done:
 if(text_sid && LocalFree(text_sid))complete=0;if(!CloseHandle(duplicate))complete=0;return complete;
}

static int verify_created_child(HANDLE process,HANDLE job,PSID expected_sid,int is_lpac) {
 HANDLE token=NULL;DWORD size=0,returned=0,is_ac=0;BOOL bound=FALSE;
 PTOKEN_APPCONTAINER_INFORMATION info=NULL;int sid_matches=0;
 BOOL ok;DWORD error;PTOKEN_GROUPS groups=NULL;int success=0;const char *query_stage="exact_job";DWORD refusal_error=0;JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits={0};
 SetLastError(0);ok=IsProcessInJob(process,job,&bound);error=GetLastError();
 fprintf(stderr,"diagnostic exact_job: api=%d bound=%d error=%lu\n",ok,bound,(unsigned long)error);
 if(!ok || !bound)goto done;
 query_stage="query_job_limits";SetLastError(0);ok=QueryInformationJobObject(job,JobObjectExtendedLimitInformation,&limits,sizeof(limits),NULL);error=GetLastError();
 fprintf(stderr,"diagnostic job_limits: api=%d error=%lu flags=%lu active=%lu memory=%llu cpu=%lld\n",ok,(unsigned long)error,(unsigned long)(ok?limits.BasicLimitInformation.LimitFlags:0),(unsigned long)(ok?limits.BasicLimitInformation.ActiveProcessLimit:0),(unsigned long long)(ok?limits.ProcessMemoryLimit:0),(long long)(ok?limits.BasicLimitInformation.PerProcessUserTimeLimit.QuadPart:0));
 if(!ok)goto done;query_stage="job_limits_expected_values";
 if(limits.BasicLimitInformation.ActiveProcessLimit!=1 || limits.ProcessMemoryLimit!=512ULL*1024*1024 ||
 limits.BasicLimitInformation.PerProcessUserTimeLimit.QuadPart!=20000000 ||
 (limits.BasicLimitInformation.LimitFlags&(JOB_OBJECT_LIMIT_PROCESS_TIME|JOB_OBJECT_LIMIT_ACTIVE_PROCESS|JOB_OBJECT_LIMIT_PROCESS_MEMORY|JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE|JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION))!=
 (JOB_OBJECT_LIMIT_PROCESS_TIME|JOB_OBJECT_LIMIT_ACTIVE_PROCESS|JOB_OBJECT_LIMIT_PROCESS_MEMORY|JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE|JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION))goto done;
 query_stage="token_open";SetLastError(0);ok=OpenProcessToken(process,TOKEN_QUERY|TOKEN_DUPLICATE,&token);error=GetLastError();
 fprintf(stderr,"diagnostic token_open: api=%d error=%lu\n",ok,(unsigned long)error);
 if(!ok)goto done;
 query_stage="class29";SetLastError(0);ok=GetTokenInformation(token,TokenIsAppContainer,&is_ac,sizeof(is_ac),&returned);error=GetLastError();
 fprintf(stderr,"diagnostic class29_control: api=%d error=%lu returned=%lu value=%lu\n",ok,(unsigned long)error,(unsigned long)returned,(unsigned long)(ok?is_ac:0));
 if(!ok || returned!=sizeof(is_ac) || is_ac!=1)goto done;
 query_stage="class31_size";SetLastError(0);ok=GetTokenInformation(token,TokenAppContainerSid,NULL,0,&size);error=GetLastError();
 fprintf(stderr,"diagnostic class31_size: api=%d error=%lu required=%lu\n",ok,(unsigned long)error,(unsigned long)size);
 if(ok || error!=ERROR_INSUFFICIENT_BUFFER || size<sizeof(*info) || size>4096)goto done;
 info=HeapAlloc(GetProcessHeap(),HEAP_ZERO_MEMORY,size);if(!info)goto done;
 query_stage="class31_value";SetLastError(0);ok=GetTokenInformation(token,TokenAppContainerSid,info,size,&returned);error=GetLastError();
 if(ok && returned>=sizeof(*info) && returned<=size && info->TokenAppContainer){
  uintptr_t begin=(uintptr_t)info,end=begin+returned,sid=(uintptr_t)info->TokenAppContainer;
  if(sid>=begin && sid<=end && end-sid>=8){
   BYTE count=((BYTE*)info->TokenAppContainer)[1];size_t sid_length=8+(size_t)count*4;
   if(count<=SID_MAX_SUB_AUTHORITIES && sid_length<=end-sid && IsValidSid(info->TokenAppContainer))sid_matches=EqualSid(info->TokenAppContainer,expected_sid);
  }
 }
 fprintf(stderr,"diagnostic class31_control: api=%d error=%lu returned=%lu sid_matches=%d\n",ok,(unsigned long)error,(unsigned long)returned,sid_matches);
 if(!ok || !sid_matches)goto done;
 size=0;
 query_stage="capabilities_size";SetLastError(0);ok=GetTokenInformation(token,TokenCapabilities,NULL,0,&size);error=GetLastError();
 fprintf(stderr,"diagnostic capabilities_size: api=%d error=%lu required=%lu\n",ok,(unsigned long)error,(unsigned long)size);
 if(ok || error!=ERROR_INSUFFICIENT_BUFFER || size<offsetof(TOKEN_GROUPS,Groups) || size>65536)goto done;
 groups=HeapAlloc(GetProcessHeap(),HEAP_ZERO_MEMORY,size);if(!groups)goto done;
 query_stage="capabilities_value";SetLastError(0);ok=GetTokenInformation(token,TokenCapabilities,groups,size,&returned);error=GetLastError();
 fprintf(stderr,"diagnostic capabilities_value: api=%d error=%lu returned=%lu count=%lu\n",ok,(unsigned long)error,(unsigned long)returned,(unsigned long)(ok && returned>=offsetof(TOKEN_GROUPS,Groups)?groups->GroupCount:0xFFFFFFFF));
 if(!ok || returned<offsetof(TOKEN_GROUPS,Groups) || returned>size || groups->GroupCount!=0)goto done;
 query_stage="policy_matrix";
 success=inspect_policy(token,expected_sid,is_lpac?"requested_lpac":"ordinary_ac",is_lpac);

 done:
 refusal_error=GetLastError();
 if(!success)fprintf(stderr,"diagnostic verification refusal: stage=%s last_error=%lu (policy mismatch may have no API error)\n",query_stage,(unsigned long)refusal_error);
 if(groups && !HeapFree(GetProcessHeap(),0,groups))success=0;
 if(info && !HeapFree(GetProcessHeap(),0,info))success=0;
 if(token && !CloseHandle(token))success=0;
 fputs("diagnostic-only: child never resumed; no LPAC attestation or execution acceptance\n",stderr);
 SetLastError(refusal_error);return success;
}
static int run_case(int argc,wchar_t **argv,int is_lpac) {
 struct owners o={0};SECURITY_ATTRIBUTES sa={sizeof(sa),NULL,TRUE};
 JOBOBJECT_EXTENDED_LIMIT_INFORMATION lim={0};SECURITY_CAPABILITIES cap={0};
 STARTUPINFOEXW si={0};PROCESS_INFORMATION pi={0};SIZE_T size=0;
 HANDLE inherited[2];DWORD optout=PROCESS_CREATION_ALL_APPLICATION_PACKAGES_OPT_OUT;
 wchar_t exe[32768],cmd[32768],systemroot[32768],localappdata[32768],env[65536];DWORD rootlen;DWORD code=99;
 int result=1,clean=1,created=0;DWORD failure_error=0;const char *stage="arguments";
 if(argc!=3 || wcschr(argv[1],L'"') || wcschr(argv[2],L'"')) {
  fputs("Synthetic probe only: profile-name existing-synthetic-canary-path\n",stderr);return 2;
 }
 /* Verify the synthetic canary exists before launch. Never read or print it. */
 {HANDLE f=CreateFileW(argv[2],GENERIC_READ,FILE_SHARE_READ,NULL,OPEN_EXISTING,0,NULL);
  if(f==INVALID_HANDLE_VALUE)return 3;if(!CloseHandle(f))return 3;}
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
 if(is_lpac && !UpdateProcThreadAttribute(o.attrs,0,PROC_THREAD_ATTRIBUTE_ALL_APPLICATION_PACKAGES_POLICY,&optout,sizeof(optout),NULL,NULL))goto done;
 stage="systemroot";SetLastError(ERROR_SUCCESS);
 rootlen=GetEnvironmentVariableW(L"SYSTEMROOT",systemroot,32768);
 if(!rootlen || rootlen>=32768)goto done;
 stage="environment_format";SetLastError(ERROR_SUCCESS);
 stage="localappdata";SetLastError(ERROR_SUCCESS);
 rootlen=GetEnvironmentVariableW(L"LOCALAPPDATA",localappdata,32768);
 if(!rootlen || rootlen>=32768)goto done;
 if(_snwprintf(env,65534,L"LOCALAPPDATA=%ls",localappdata)<0)goto done;
 {size_t offset=wcslen(env)+1;
  if(_snwprintf(env+offset,65534-offset,L"SYSTEMROOT=%ls",systemroot)<0)goto done;
  env[offset+wcslen(env+offset)+1]=L'\0';}

 /* Explicit LOCALAPPDATA + SYSTEMROOT block, final double NUL above. */
 stage="self_exe";SetLastError(ERROR_SUCCESS);
 rootlen=GetModuleFileNameW(NULL,exe,32768);if(!rootlen || rootlen>=32768)goto done;
 stage="command_format";SetLastError(ERROR_SUCCESS);
 if(_snwprintf(cmd,32768,L"\"%ls\" --never-execute \"%ls\"",exe,argv[2])<0)goto done;
 si.StartupInfo.cb=sizeof(si);si.lpAttributeList=o.attrs;si.StartupInfo.dwFlags=STARTF_USESTDHANDLES;
 si.StartupInfo.hStdInput=o.nul;si.StartupInfo.hStdOutput=o.write_pipe;si.StartupInfo.hStdError=o.nul;
 /* Output pair is written directly into the stack-owned pi structure by OS.
    No Python callback/adoption exists. Caller remains single native process. */
 stage="create_suspended_child";SetLastError(ERROR_SUCCESS);
 if(!CreateProcessW(exe,cmd,NULL,NULL,TRUE,CREATE_SUSPENDED|CREATE_NO_WINDOW|CREATE_UNICODE_ENVIRONMENT|EXTENDED_STARTUPINFO_PRESENT,
 env,NULL,&si.StartupInfo,&pi))goto done;
 created=1;o.process=pi.hProcess;o.thread=pi.hThread;
 stage="verify_child";SetLastError(ERROR_SUCCESS);
 if(!verify_created_child(o.process,o.job,o.sid,is_lpac))goto done;
 result=0;
 fputs("policy case matched matrix; child remains suspended\n",stderr);

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
 if(o.attrs && !HeapFree(GetProcessHeap(),0,o.attrs))clean=0;if(o.sid && FreeSid(o.sid))clean=0;
 if(!clean)result=1;
 return result;
}

int wmain(int argc,wchar_t **argv) {
 /* Accidental executable launch cannot recursively create descendants. */
 if(argc>=2 && !wcscmp(argv[1],L"--never-execute"))return 125;
 /* No child dispatch and no ResumeThread path anywhere in this diagnostic. */
 if(run_case(argc,argv,0))return 1;
 if(run_case(argc,argv,1))return 1;
 fputs("both suspended policy controls matched; not parser/file/network acceptance\n",stderr);
 return 0;
}
