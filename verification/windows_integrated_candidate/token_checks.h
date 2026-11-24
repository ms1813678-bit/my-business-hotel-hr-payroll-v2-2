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
