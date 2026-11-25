/* Fixed-list real open/section measurements. NOT actual-child loader evidence. */
struct real_open_result { DWORD open_error,section_error; int opened,section,clean; };
struct real_open_context { HANDLE token; wchar_t paths[15][32768]; struct real_open_result results[15]; int verified,reverted,clean; DWORD error; };
static int same_luid(LUID a,LUID b){return a.LowPart==b.LowPart && a.HighPart==b.HighPart;}
static DWORD WINAPI real_open_thread(LPVOID opaque){
 struct real_open_context *c=opaque;HANDLE actual=NULL;TOKEN_STATISTICS expected={0},observed={0};DWORD returned=0;c->clean=1;
 if(!SetThreadToken(NULL,c->token)){c->error=GetLastError();return 1;}
 if(!OpenThreadToken(GetCurrentThread(),TOKEN_QUERY,TRUE,&actual)){c->error=GetLastError();goto done;}
 if(!GetTokenInformation(c->token,TokenStatistics,&expected,sizeof(expected),&returned) || !GetTokenInformation(actual,TokenStatistics,&observed,sizeof(observed),&returned)){c->error=GetLastError();goto done;}
 if(!same_luid(expected.TokenId,observed.TokenId) || observed.TokenType!=TokenImpersonation || observed.ImpersonationLevel!=SecurityImpersonation){c->error=ERROR_BAD_IMPERSONATION_LEVEL;goto done;}
 c->verified=1;
 for(unsigned int i=0;i<15;i++){
  struct real_open_result *r=&c->results[i];r->clean=1;SetLastError(0);
  HANDLE file=CreateFileW(c->paths[i],GENERIC_READ|GENERIC_EXECUTE,FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE,NULL,OPEN_EXISTING,0,NULL);
  if(file==INVALID_HANDLE_VALUE){r->open_error=GetLastError();continue;}r->opened=1;
  SetLastError(0);HANDLE section=CreateFileMappingW(file,NULL,PAGE_READONLY|SEC_IMAGE,0,0,NULL);
  if(!section)r->section_error=GetLastError();else {r->section=1;if(!CloseHandle(section))r->clean=0;}
  if(!CloseHandle(file))r->clean=0;if(!r->clean)c->clean=0;
 }
 done:
 if(actual && !CloseHandle(actual))c->clean=0;
 if(!RevertToSelf())ExitProcess(126);
 actual=NULL;SetLastError(0);if(OpenThreadToken(GetCurrentThread(),TOKEN_QUERY,TRUE,&actual) || GetLastError()!=ERROR_NO_TOKEN){if(actual)CloseHandle(actual);ExitProcess(126);}
 c->reverted=1;
 return c->verified&&c->clean?0:1;
}
static int diagnose_real_open(HANDLE process,const wchar_t *runtime){
 HANDLE primary=NULL,token=NULL,thread=NULL;int clean=1,ok=0;wchar_t system[32768];
 const wchar_t *files[]={L"python.exe",L"python314.dll",L"vcruntime140.dll",L"vcruntime140_1.dll",L"ntdll.dll",L"kernel32.dll",L"kernelbase.dll",L"ucrtbase.dll",L"version.dll",L"ws2_32.dll",L"bcrypt.dll",L"bcryptprimitives.dll",L"advapi32.dll",L"sechost.dll",L"rpcrt4.dll"};
 const char *labels[]={"runtime_python_exe","runtime_python314","runtime_vcruntime140","runtime_vcruntime140_1","system_ntdll","system_kernel32","system_kernelbase","system_ucrtbase","system_version","system_ws2_32","system_bcrypt","system_bcryptprimitives","system_advapi32","system_sechost","system_rpcrt4"};
 struct real_open_context *c=HeapAlloc(GetProcessHeap(),HEAP_ZERO_MEMORY,sizeof(*c));if(!c)return 0;
 UINT n=GetSystemDirectoryW(system,32768);if(!n||n>=32768)goto done;
 for(unsigned int i=0;i<15;i++)if(_snwprintf_s(c->paths[i],32768,_TRUNCATE,L"%ls\\%ls",i<4?runtime:system,files[i])<0)goto done;
 if(!OpenProcessToken(process,TOKEN_QUERY|TOKEN_DUPLICATE,&primary))goto done;
 if(!DuplicateTokenEx(primary,TOKEN_QUERY|TOKEN_IMPERSONATE,NULL,SecurityImpersonation,TokenImpersonation,&token))goto done;
 c->token=token;thread=CreateThread(NULL,0,real_open_thread,c,0,NULL);if(!thread)goto done;
 if(WaitForSingleObject(thread,10000)!=WAIT_OBJECT_0){fputs("startup_real_open_refusal thread_join_unconfirmed=1\n",stderr);ExitProcess(127);}
 fprintf(stderr,"startup_real_open_context actual_thread_token_verified=%d reverted=%d clean=%d error=%lu parent_process_context=1\n",c->verified,c->reverted,c->clean,(unsigned long)c->error);
 if(!c->verified||!c->reverted||!c->clean)goto done;
 for(unsigned int i=0;i<15;i++){
  struct real_open_result *r=&c->results[i];
  fprintf(stderr,"startup_real_open label=%s opened=%d open_error=%lu section_created=%d section_error=%lu clean=%d fixed_list_only=1 no_map_or_execution=1 no_loader_clearance=1\n",labels[i],r->opened,(unsigned long)r->open_error,r->section,(unsigned long)r->section_error,r->clean);
 }
 ok=1;
 done:
 if(thread&&!CloseHandle(thread))clean=0;if(token&&!CloseHandle(token))clean=0;if(primary&&!CloseHandle(primary))clean=0;
 if(!HeapFree(GetProcessHeap(),0,c))clean=0;
 fprintf(stderr,"startup_real_open_cleanup complete=%d measurement_valid=%d\n",clean,ok);return clean&&ok;
}
