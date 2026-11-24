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
#include <aclapi.h>
#include <stdio.h>
#include <stdint.h>
#include <stddef.h>
#include "fixed_child_resources.c"
#include "positive_loopback_controls.c"
#include <wchar.h>
#ifndef PROC_THREAD_ATTRIBUTE_JOB_LIST
#define PROC_THREAD_ATTRIBUTE_JOB_LIST ProcThreadAttributeValue(ProcThreadAttributeJobList,FALSE,TRUE,FALSE)
#endif
static long fault_target=0,fault_counter=0;
static int injected_fault(const char *stage){fault_counter++;if(fault_target==fault_counter){fprintf(stderr,"INJECTED_FAULT step=%ld stage=%s\n",fault_counter,stage);SetLastError(ERROR_CANCELLED);return 1;}return 0;}
struct owners {
 HANDLE completion,job,read_pipe,write_pipe,nul,process,thread;
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
static int security_snapshot(HANDLE file,LPWSTR *text){
 PSECURITY_DESCRIPTOR sd=NULL;DWORD flags=OWNER_SECURITY_INFORMATION|GROUP_SECURITY_INFORMATION|DACL_SECURITY_INFORMATION|LABEL_SECURITY_INFORMATION;
 DWORD error=GetSecurityInfo(file,SE_FILE_OBJECT,flags,NULL,NULL,NULL,NULL,&sd);
 if(error!=ERROR_SUCCESS)return 0;
 BOOL ok=ConvertSecurityDescriptorToStringSecurityDescriptorW(sd,SDDL_REVISION_1,flags,text,NULL);
 if(LocalFree(sd))ok=FALSE;
 return ok;
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
 if(_snwprintf_s(profile,512,_TRUNCATE,L"O:SYG:SYD:(A;;FR;;;WD)(A;;FR;;;%ls)",text_sid)<0){complete=0;goto done;}
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
 fputs("inspection checkpoint: child is suspended here; this token/policy check alone is not execution acceptance\n",stderr);
 SetLastError(refusal_error);return success;
}
static int run_case(int argc,wchar_t **argv,int is_lpac) {
 struct owners o={0};SECURITY_ATTRIBUTES sa={sizeof(sa),NULL,TRUE};
 JOBOBJECT_EXTENDED_LIMIT_INFORMATION lim={0};SECURITY_CAPABILITIES cap={0};
 STARTUPINFOEXW si={0};PROCESS_INFORMATION pi={0};SIZE_T size=0;
 HANDLE inherited[2];DWORD optout=PROCESS_CREATION_ALL_APPLICATION_PACKAGES_OPT_OUT;
 wchar_t exe[32768],cmd[32768],systemroot[32768],localappdata[32768],env[65536];DWORD rootlen;DWORD code=99;
 LPWSTR canary_sd_before=NULL,canary_sd_after=NULL;
 HANDLE canary_guard=NULL;struct listener_control listeners[4]={{0}};int listener_count=0;const int families[4]={AF_INET,AF_INET6,AF_INET,AF_INET6};WSADATA wsa;int wsa_started=0;
 int result=1,clean=1,created=0;int park_mode=0;wchar_t park[8];DWORD parklen=GetEnvironmentVariableW(L"HONY_SYNTHETIC_PARK",park,8);if(parklen && !wcscmp(park,L"1"))park_mode=1;DWORD failure_error=0;const char *stage="arguments";
 if(argc!=3 || wcschr(argv[1],L'"') || wcschr(argv[2],L'"')) {
  fputs("Synthetic probe only: profile-name existing-synthetic-canary-path\n",stderr);return 2;
 }
 /* Parent canary positive control; deny replacement/writers until child ends. */
 BY_HANDLE_FILE_INFORMATION canary_identity={0};
 wchar_t canary[32768];if(_snwprintf_s(canary,32768,_TRUNCATE,L"%ls\\private\\canary.txt",argv[2])<0)return 3;
 canary_guard=CreateFileW(canary,GENERIC_READ|READ_CONTROL,FILE_SHARE_READ,NULL,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,NULL);
 if(canary_guard==INVALID_HANDLE_VALUE)return 3;
 if(!GetFileInformationByHandle(canary_guard,&canary_identity) || !security_snapshot(canary_guard,&canary_sd_before))goto done;
 {char marker[64]={0};DWORD n=0;static const char expected[]="HONY-SYNTHETIC-FORBIDDEN-v1";
 if(!ReadFile(canary_guard,marker,sizeof(marker),&n,NULL) || n!=sizeof(expected)-1 || memcmp(marker,expected,sizeof(expected)-1))goto done;}
 if(is_lpac){
  stage="loopback_positive_controls";if(WSAStartup(MAKEWORD(2,2),&wsa))goto done;wsa_started=1;
  for(int i=0;i<4;i++){
   if(!setup_positive_listener(&listeners[i],families[i],i>=2,"HONY-UNCONTAINED-POSITIVE-CONTROL-v1"))goto done;
   listener_count++;fprintf(stderr,"positive_listener family=%d datagram=%d port=%u nonce_ack=1\n",families[i],i>=2,listeners[i].port);
  }
 }
 stage="derive_sid";if(injected_fault(stage))goto done;SetLastError(ERROR_SUCCESS);
 if(FAILED(DeriveAppContainerSidFromAppContainerName(argv[1],&o.sid)))goto done;
 stage="create_job";if(injected_fault(stage))goto done;SetLastError(ERROR_SUCCESS);
 o.job=CreateJobObjectW(NULL,NULL);if(!o.job)goto done;
 lim.BasicLimitInformation.LimitFlags=JOB_OBJECT_LIMIT_PROCESS_TIME|JOB_OBJECT_LIMIT_ACTIVE_PROCESS|
 JOB_OBJECT_LIMIT_PROCESS_MEMORY|JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE|JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION;
 lim.BasicLimitInformation.PerProcessUserTimeLimit.QuadPart=20000000;lim.BasicLimitInformation.ActiveProcessLimit=1;
 lim.ProcessMemoryLimit=512ULL*1024*1024;
 stage="configure_job";if(injected_fault(stage))goto done;SetLastError(ERROR_SUCCESS);
 if(!SetInformationJobObject(o.job,JobObjectExtendedLimitInformation,&lim,sizeof(lim)))goto done;
 if(is_lpac){
  JOBOBJECT_ASSOCIATE_COMPLETION_PORT assoc={0};
  stage="create_completion_port";if(injected_fault(stage))goto done;
  o.completion=CreateIoCompletionPort(INVALID_HANDLE_VALUE,NULL,0,1);if(!o.completion)goto done;
  assoc.CompletionKey=&o;assoc.CompletionPort=o.completion;
  stage="associate_completion_port";if(injected_fault(stage))goto done;
  if(!SetInformationJobObject(o.job,JobObjectAssociateCompletionPortInformation,&assoc,sizeof(assoc)))goto done;
 }
 stage="create_pipe";if(injected_fault(stage))goto done;SetLastError(ERROR_SUCCESS);
 if(!CreatePipe(&o.read_pipe,&o.write_pipe,&sa,0))goto done;
 stage="read_noinherit";if(injected_fault(stage))goto done;SetLastError(ERROR_SUCCESS);
 if(!SetHandleInformation(o.read_pipe,HANDLE_FLAG_INHERIT,0))goto done;
 stage="open_nul";if(injected_fault(stage))goto done;SetLastError(ERROR_SUCCESS);
 o.nul=CreateFileW(L"NUL",GENERIC_READ|GENERIC_WRITE,FILE_SHARE_READ|FILE_SHARE_WRITE,&sa,OPEN_EXISTING,0,NULL);
 if(o.nul==INVALID_HANDLE_VALUE)goto done;
 stage="attribute_size";if(injected_fault(stage))goto done;SetLastError(ERROR_SUCCESS);
 InitializeProcThreadAttributeList(NULL,4,0,&size);if(!size)goto done;
 stage="attribute_allocate";if(injected_fault(stage))goto done;SetLastError(ERROR_SUCCESS);
 o.attrs=HeapAlloc(GetProcessHeap(),HEAP_ZERO_MEMORY,size);if(!o.attrs)goto done;
 stage="attribute_init";if(injected_fault(stage))goto done;SetLastError(ERROR_SUCCESS);
 if(!InitializeProcThreadAttributeList(o.attrs,4,0,&size))goto done;o.attrs_initialized=1;
 inherited[0]=o.nul;inherited[1]=o.write_pipe;
 cap.AppContainerSid=o.sid;cap.Capabilities=NULL;cap.CapabilityCount=0;
 stage="attribute_handles";if(injected_fault(stage))goto done;SetLastError(ERROR_SUCCESS);
 if(!UpdateProcThreadAttribute(o.attrs,0,PROC_THREAD_ATTRIBUTE_HANDLE_LIST,inherited,sizeof(inherited),NULL,NULL))goto done;
 stage="attribute_job";if(injected_fault(stage))goto done;SetLastError(ERROR_SUCCESS);
 if(!UpdateProcThreadAttribute(o.attrs,0,PROC_THREAD_ATTRIBUTE_JOB_LIST,&o.job,sizeof(o.job),NULL,NULL))goto done;
 stage="attribute_capabilities";if(injected_fault(stage))goto done;SetLastError(ERROR_SUCCESS);
 if(!UpdateProcThreadAttribute(o.attrs,0,PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,&cap,sizeof(cap),NULL,NULL))goto done;
 stage="attribute_lpac";if(injected_fault(stage))goto done;SetLastError(ERROR_SUCCESS);
 if(is_lpac && !UpdateProcThreadAttribute(o.attrs,0,PROC_THREAD_ATTRIBUTE_ALL_APPLICATION_PACKAGES_POLICY,&optout,sizeof(optout),NULL,NULL))goto done;
 stage="systemroot";if(injected_fault(stage))goto done;SetLastError(ERROR_SUCCESS);
 rootlen=GetEnvironmentVariableW(L"SYSTEMROOT",systemroot,32768);
 if(!rootlen || rootlen>=32768)goto done;
 stage="environment_format";SetLastError(ERROR_SUCCESS);
 stage="localappdata";SetLastError(ERROR_SUCCESS);
 rootlen=GetEnvironmentVariableW(L"LOCALAPPDATA",localappdata,32768);
 if(!rootlen || rootlen>=32768)goto done;
 if(_snwprintf_s(env,65534,_TRUNCATE,L"LOCALAPPDATA=%ls",localappdata)<0)goto done;
 {size_t offset=wcslen(env)+1;
  if(_snwprintf_s(env+offset,65534-offset,_TRUNCATE,L"SYSTEMROOT=%ls",systemroot)<0)goto done;
  env[offset+wcslen(env+offset)+1]=L'\0';}

 /* Explicit LOCALAPPDATA + SYSTEMROOT block, final double NUL above. */
 stage="self_exe";SetLastError(ERROR_SUCCESS);
 rootlen=GetModuleFileNameW(NULL,exe,32768);if(!rootlen || rootlen>=32768)goto done;
 stage="command_format";SetLastError(ERROR_SUCCESS);
 if(is_lpac){
 if(_snwprintf_s(cmd,32768,_TRUNCATE,L"\"%ls\" --resource-child \"%ls\" %u %u %u %u",exe,argv[2],(unsigned int)listeners[0].port,(unsigned int)listeners[1].port,(unsigned int)listeners[2].port,(unsigned int)listeners[3].port)<0)goto done;
 }else if(_snwprintf_s(cmd,32768,_TRUNCATE,L"\"%ls\" --never-execute",exe)<0)goto done;
 si.StartupInfo.cb=sizeof(si);si.lpAttributeList=o.attrs;si.StartupInfo.dwFlags=STARTF_USESTDHANDLES;
 si.StartupInfo.hStdInput=o.nul;si.StartupInfo.hStdOutput=o.write_pipe;si.StartupInfo.hStdError=o.nul;
 /* Output pair is written directly into the stack-owned pi structure by OS.
    No Python callback/adoption exists. Caller remains single native process. */
 stage="create_suspended_child";if(injected_fault(stage))goto done;SetLastError(ERROR_SUCCESS);
 if(!CreateProcessW(exe,cmd,NULL,NULL,TRUE,CREATE_SUSPENDED|CREATE_NO_WINDOW|CREATE_UNICODE_ENVIRONMENT|EXTENDED_STARTUPINFO_PRESENT,
 env,NULL,&si.StartupInfo,&pi))goto done;
 created=1;o.process=pi.hProcess;o.thread=pi.hThread;
 stage="verify_child";if(injected_fault(stage))goto done;SetLastError(ERROR_SUCCESS);
 if(!verify_created_child(o.process,o.job,o.sid,is_lpac))goto done;
 if(is_lpac && park_mode){FILETIME created_at,exit_at,kernel,user;if(!GetProcessTimes(o.process,&created_at,&exit_at,&kernel,&user))goto done;
 fprintf(stdout,"PARKED_CHILD pid=%lu created=%llu\n",(unsigned long)GetProcessId(o.process),(unsigned long long)(((ULONGLONG)created_at.dwHighDateTime<<32)|created_at.dwLowDateTime));fflush(stdout);
 Sleep(30000);SetLastError(ERROR_TIMEOUT);goto done;}
 if(!is_lpac){result=0;fputs("ordinary policy control matched; remains suspended\n",stderr);goto done;}
 for(int i=0;i<4;i++){
  GUID nonce_guid;char nonce[64];if(CoCreateGuid(&nonce_guid)!=S_OK)goto done;
  if(snprintf(nonce,sizeof(nonce),"%08lx%04x%04x%02x%02x%02x%02x%02x%02x%02x%02x",(unsigned long)nonce_guid.Data1,nonce_guid.Data2,nonce_guid.Data3,nonce_guid.Data4[0],nonce_guid.Data4[1],nonce_guid.Data4[2],nonce_guid.Data4[3],nonce_guid.Data4[4],nonce_guid.Data4[5],nonce_guid.Data4[6],nonce_guid.Data4[7])!=32)goto done;
  if(!verify_positive_listener(&listeners[i],nonce))goto done;
 }
 stage="close_parent_writer";if(injected_fault(stage))goto done;close_once(&o.write_pipe,&clean);if(!clean)goto done;
 stage="resume_fixed_child";if(injected_fault(stage))goto done;if(ResumeThread(o.thread)!=1)goto done;
 stage="bounded_child_wait";if(WaitForSingleObject(o.process,3000)!=WAIT_OBJECT_0)goto done;
 stage="child_exit_query";if(!GetExitCodeProcess(o.process,&code))goto done;
 {char output[2049]={0};DWORD available=0,got=0;stage="bounded_output";
 if(!PeekNamedPipe(o.read_pipe,NULL,0,NULL,&available,NULL)||available>2048)goto done;
 if(available && (!ReadFile(o.read_pipe,output,available,&got,NULL)||got!=available))goto done;
 fwrite(output,1,got,stdout);}
 stage="child_exit";if(code!=0)goto done;
 {int notified=0;ULONGLONG deadline=GetTickCount64()+1000;stage="active_process_limit_notification";
  while(GetTickCount64()<deadline){DWORD message=0;ULONG_PTR key=0;LPOVERLAPPED payload=NULL;
   if(!GetQueuedCompletionStatus(o.completion,&message,&key,&payload,50)){if(GetLastError()==WAIT_TIMEOUT)continue;goto done;}
   if(key!=(ULONG_PTR)&o)goto done;
   if(message==JOB_OBJECT_MSG_ACTIVE_PROCESS_LIMIT && payload==NULL){notified=1;break;}
  }
  if(!notified)goto done;
  fputs("Exact owned Job ACTIVE_PROCESS_LIMIT notification observed\n",stderr);
 }
 {BY_HANDLE_FILE_INFORMATION after={0};stage="canary_identity_after";
 if(!GetFileInformationByHandle(canary_guard,&after) || after.dwVolumeSerialNumber!=canary_identity.dwVolumeSerialNumber || after.nFileIndexHigh!=canary_identity.nFileIndexHigh || after.nFileIndexLow!=canary_identity.nFileIndexLow)goto done;}
 if(!security_snapshot(canary_guard,&canary_sd_after) || wcscmp(canary_sd_before,canary_sd_after))goto done;
 result=0;
 fputs("fixed synthetic child returned all enforced resource checks; no Python parser acceptance\n",stderr);

 done:
 failure_error=GetLastError();
 if(result!=0)fprintf(stderr,"Synthetic probe refusal: stage %s; Win32 error %lu; child code %lu\n",stage,(unsigned long)failure_error,(unsigned long)code);
 if(created){
  FILETIME created_at,exit_at,kernel,user;
  if(GetProcessTimes(o.process,&created_at,&exit_at,&kernel,&user))fprintf(stderr,"OWNED_CHILD pid=%lu created=%llu cleanup=begin\n",(unsigned long)GetProcessId(o.process),(unsigned long long)(((ULONGLONG)created_at.dwHighDateTime<<32)|created_at.dwLowDateTime));
  if(!TerminateJobObject(o.job,99))clean=0;
  if(WaitForSingleObject(o.process,2000)!=WAIT_OBJECT_0)clean=0;
  fprintf(stderr,"OWNED_CHILD cleanup_verified=%d\n",clean);
 }
 close_once(&o.thread,&clean);close_once(&o.process,&clean);
 close_once(&o.write_pipe,&clean);close_once(&o.read_pipe,&clean);close_once(&o.nul,&clean);close_once(&o.job,&clean);close_once(&o.completion,&clean);
 if(o.attrs_initialized)DeleteProcThreadAttributeList(o.attrs);
 if(o.attrs && !HeapFree(GetProcessHeap(),0,o.attrs))clean=0;if(o.sid && FreeSid(o.sid))clean=0;
 for(int i=0;i<listener_count;i++)if(closesocket(listeners[i].listener))clean=0;
 if(wsa_started && WSACleanup())clean=0;
 if(canary_guard && canary_guard!=INVALID_HANDLE_VALUE && !CloseHandle(canary_guard))clean=0;
 if(canary_sd_before && LocalFree(canary_sd_before))clean=0;
 if(canary_sd_after && LocalFree(canary_sd_after))clean=0;
 fprintf(stderr,"NATIVE_CLEANUP complete=%d\n",clean);
 if(!clean)result=1;
 return result;
}

static int fixed_child(int argc,wchar_t **argv){
 if(argc!=7){child_diag("argc",argc,0);return 125;}
 wchar_t allowed[32768],forbidden[32768],scratch[32768],blocked[32768];unsigned short ports[4];
 for(int i=0;i<4;i++){wchar_t *end=NULL;unsigned long p=wcstoul(argv[3+i],&end,10);if(!end||*end||!p||p>65535){child_diag("port_argument",i,0);return 125;}ports[i]=(unsigned short)p;}
 errno=0;
 if(_snwprintf_s(allowed,32768,_TRUNCATE,L"%ls\\runtime\\allowed.txt",argv[2])<0 ||
 _snwprintf_s(forbidden,32768,_TRUNCATE,L"%ls\\private\\canary.txt",argv[2])<0 ||
 _snwprintf_s(scratch,32768,_TRUNCATE,L"%ls\\scratch\\created.txt",argv[2])<0 ||
 _snwprintf_s(blocked,32768,_TRUNCATE,L"%ls\\private\\must-not-exist.txt",argv[2])<0){child_diag("path_format",-1,(unsigned long)errno);return 125;}
 struct resource_paths paths={allowed,forbidden,scratch,blocked};
 int rc=run_fixed_child_resources(&paths,ports);
 wchar_t exe[32768],cmd[32768];DWORD n=GetModuleFileNameW(NULL,exe,32768);if(!n||n>=32768){child_diag("GetModuleFileNameW",(long)n,(unsigned long)GetLastError());return 1;}
 errno=0;
 if(_snwprintf_s(cmd,32768,_TRUNCATE,L"\"%ls\" --never-execute",exe)<0){child_diag("command_format",-1,(unsigned long)errno);return 1;}
 STARTUPINFOW si={0};PROCESS_INFORMATION pi={0};si.cb=sizeof(si);SetLastError(0);
 BOOL created=CreateProcessW(exe,cmd,NULL,NULL,FALSE,CREATE_SUSPENDED|CREATE_NO_WINDOW,NULL,NULL,&si,&pi);DWORD err=GetLastError();
 fprintf(stdout,"resource test=fixed_child_process_creation_denied created=%d error=%lu\n",created,(unsigned long)err);
 if(created){
 BOOL terminated=TerminateProcess(pi.hProcess,125);DWORD terminate_error=terminated?0:GetLastError();child_diag("unexpected_child_terminate",terminated,(unsigned long)terminate_error);
 DWORD waited=WaitForSingleObject(pi.hProcess,1000);DWORD wait_error=waited==WAIT_FAILED?GetLastError():0;child_diag("unexpected_child_wait",(long)waited,(unsigned long)wait_error);
 child_close_handle(pi.hThread);child_close_handle(pi.hProcess);child_diag("fixed_child_return",1,0);child_flush();return 1;}
 if(err!=ERROR_ACCESS_DENIED && err!=ERROR_NOT_ENOUGH_QUOTA){child_diag("process_denial_rejected",0,(unsigned long)err);return 1;}
 child_diag("fixed_child_return",rc,0);
 if(child_flush())return 1;
 return rc;
}
int wmain(int argc,wchar_t **argv) {
 if(argc>=2 && !wcscmp(argv[1],L"--never-execute"))return 125;
 if(argc>=2 && !wcscmp(argv[1],L"--resource-child"))return fixed_child(argc,argv);
 wchar_t fault[32];DWORD fl=GetEnvironmentVariableW(L"HONY_SYNTHETIC_FAULT",fault,32);
 if(fl && fl<32){wchar_t *end=NULL;fault_target=wcstol(fault,&end,10);if(!end||*end||fault_target<1||fault_target>128)return 2;}
 if(!fault_target && run_case(argc,argv,0))return 1;
 if(run_case(argc,argv,1))return 1;
 return 0;
}
