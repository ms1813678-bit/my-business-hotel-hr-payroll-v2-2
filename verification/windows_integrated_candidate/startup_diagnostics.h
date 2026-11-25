/* Read-only advisory file-DACL evidence, never a loader trace or policy grant. */
#include <aclapi.h>
static int diagnose_file(HANDLE token,const wchar_t *path,const char *label,DWORD mask){
 PSECURITY_DESCRIPTOR sd=NULL;PRIVILEGE_SET initial={0};PPRIVILEGE_SET ps=&initial;
 DWORD need=sizeof(initial),granted=0,error=0;BOOL allowed=FALSE,ok=FALSE;
 GENERIC_MAPPING map={FILE_GENERIC_READ,FILE_GENERIC_WRITE,FILE_GENERIC_EXECUTE,FILE_ALL_ACCESS};
 DWORD rc=GetNamedSecurityInfoW((LPWSTR)path,SE_FILE_OBJECT,OWNER_SECURITY_INFORMATION|GROUP_SECURITY_INFORMATION|DACL_SECURITY_INFORMATION,NULL,NULL,NULL,NULL,&sd);
 if(rc==ERROR_SUCCESS){
  MapGenericMask(&mask,&map);SetLastError(0);ok=AccessCheck(sd,token,mask,&map,ps,&need,&granted,&allowed);error=ok?0:GetLastError();
  if(!ok && error==ERROR_INSUFFICIENT_BUFFER && need>sizeof(initial) && need<=65536){
   ps=HeapAlloc(GetProcessHeap(),HEAP_ZERO_MEMORY,need);
   if(ps){SetLastError(0);ok=AccessCheck(sd,token,mask,&map,ps,&need,&granted,&allowed);error=ok?0:GetLastError();}
   else error=ERROR_NOT_ENOUGH_MEMORY;
  }
 }
 fprintf(stderr,"startup_file_dacl label=%s descriptor_error=%lu access_api=%d allowed=%d granted=%lu api_error=%lu mask=%lu advisory_only=1\n",label,(unsigned long)rc,ok,allowed,(unsigned long)granted,(unsigned long)error,(unsigned long)mask);
 int clean=1;if(ps!=&initial && ps && !HeapFree(GetProcessHeap(),0,ps))clean=0;if(sd && LocalFree(sd))clean=0;return clean;
}
static int diagnose_startup(HANDLE process,const wchar_t *runtime){
 HANDLE primary=NULL,token=NULL;int clean=1;DWORD n=0;PTOKEN_PRIVILEGES privileges=NULL;LUID traverse;
 wchar_t path[32768],system[32768];const wchar_t *bundled[]={L"python.exe",L"python314.dll",L"vcruntime140.dll",L"vcruntime140_1.dll"};
 const char *bundled_labels[]={"runtime_python_exe","runtime_python314","runtime_vcruntime140","runtime_vcruntime140_1"};
 const wchar_t *dlls[]={L"ntdll.dll",L"kernel32.dll",L"kernelbase.dll",L"ucrtbase.dll",L"version.dll",L"ws2_32.dll",L"bcrypt.dll",L"bcryptprimitives.dll",L"advapi32.dll",L"sechost.dll",L"rpcrt4.dll"};
 const char *labels[]={"system_ntdll","system_kernel32","system_kernelbase","system_ucrtbase","system_version","system_ws2_32","system_bcrypt","system_bcryptprimitives","system_advapi32","system_sechost","system_rpcrt4"};
 if(!OpenProcessToken(process,TOKEN_QUERY|TOKEN_DUPLICATE,&primary)){fprintf(stderr,"startup_diagnostic_refusal stage=token_open error=%lu\n",(unsigned long)GetLastError());return 0;}
 if(!DuplicateToken(primary,SecurityImpersonation,&token)){fprintf(stderr,"startup_diagnostic_refusal stage=token_duplicate error=%lu\n",(unsigned long)GetLastError());clean=0;goto done;}
 if(!LookupPrivilegeValueW(NULL,SE_CHANGE_NOTIFY_NAME,&traverse)){clean=0;goto done;}
 SetLastError(0);BOOL sized=GetTokenInformation(primary,TokenPrivileges,NULL,0,&n);DWORD e=GetLastError();
 if(sized || e!=ERROR_INSUFFICIENT_BUFFER || n<sizeof(DWORD) || n>65536){clean=0;goto done;}
 privileges=HeapAlloc(GetProcessHeap(),HEAP_ZERO_MEMORY,n);if(!privileges){clean=0;goto done;}
 DWORD returned=0;if(!GetTokenInformation(primary,TokenPrivileges,privileges,n,&returned) || returned<sizeof(DWORD) || returned>n || privileges->PrivilegeCount>(returned-sizeof(DWORD))/sizeof(LUID_AND_ATTRIBUTES)){clean=0;goto done;}
 int present=0,enabled=0;for(DWORD i=0;i<privileges->PrivilegeCount;i++)if(privileges->Privileges[i].Luid.LowPart==traverse.LowPart && privileges->Privileges[i].Luid.HighPart==traverse.HighPart){present=1;enabled=(privileges->Privileges[i].Attributes&SE_PRIVILEGE_ENABLED)!=0;}
 fprintf(stderr,"startup_privilege traverse_present=%d traverse_enabled=%d privilege_count=%lu advisory_only=1\n",present,enabled,(unsigned long)privileges->PrivilegeCount);
 for(unsigned int i=0;i<4;i++){
  if(_snwprintf_s(path,32768,_TRUNCATE,L"%ls\\%ls",runtime,bundled[i])<0){clean=0;goto done;}
  if(!diagnose_file(token,path,bundled_labels[i],GENERIC_READ|GENERIC_EXECUTE)){clean=0;goto done;}
 }
 /* Directory access is independent from file RX. No ancestor is granted access. */
 if(wcslen(runtime)>=32768){clean=0;goto done;}wcscpy_s(path,32768,runtime);
 for(unsigned int depth=0;depth<16;depth++){
  char label[40];if(sprintf_s(label,sizeof(label),"runtime_ancestor_%u",depth)<0){clean=0;goto done;}
  if(!diagnose_file(token,path,label,FILE_TRAVERSE)){clean=0;goto done;}
  size_t len=wcslen(path);if(len==3 && path[1]==L':' && path[2]==L'\\')break;
  wchar_t *slash=wcsrchr(path,L'\\');if(!slash){clean=0;goto done;}if(slash==path+2)slash[1]=0;else *slash=0;
  if(depth==15){clean=0;goto done;}
 }
 UINT count=GetSystemDirectoryW(system,32768);if(!count || count>=32768){clean=0;goto done;}
 for(unsigned int i=0;i<sizeof(dlls)/sizeof(dlls[0]);i++){
  if(_snwprintf_s(path,32768,_TRUNCATE,L"%ls\\%ls",system,dlls[i])<0){clean=0;goto done;}
  if(!diagnose_file(token,path,labels[i],GENERIC_READ|GENERIC_EXECUTE)){clean=0;goto done;}
 }
 fprintf(stderr,"startup_diagnostic_scope file_dacl_only=1 omits_mandatory_policy_sections_registry_loader_resolution=1 no_causal_claim=1\n");
 done:
 if(privileges && !HeapFree(GetProcessHeap(),0,privileges))clean=0;if(token && !CloseHandle(token))clean=0;if(primary && !CloseHandle(primary))clean=0;
 fprintf(stderr,"startup_diagnostic_cleanup complete=%d\n",clean);return clean;
}
static void diagnose_exit(HANDLE process){
 DWORD code=0;SetLastError(0);BOOL exit_ok=GetExitCodeProcess(process,&code);DWORD exit_error=exit_ok?0:GetLastError();
 FILETIME created={0},exited={0},kernel={0},user={0};SetLastError(0);BOOL times_ok=GetProcessTimes(process,&created,&exited,&kernel,&user);DWORD times_error=times_ok?0:GetLastError();
 ULARGE_INTEGER k,u;k.LowPart=kernel.dwLowDateTime;k.HighPart=kernel.dwHighDateTime;u.LowPart=user.dwLowDateTime;u.HighPart=user.dwHighDateTime;
 fprintf(stderr,"startup_process_exit api=%d error=%lu code=%lu still_active=%d times_api=%d times_error=%lu kernel_100ns=%llu user_100ns=%llu before_cleanup_termination=1\n",exit_ok,(unsigned long)exit_error,(unsigned long)code,exit_ok&&code==STILL_ACTIVE,times_ok,(unsigned long)times_error,(unsigned long long)k.QuadPart,(unsigned long long)u.QuadPart);
}
