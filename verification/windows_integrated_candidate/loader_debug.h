/* REVIEW CANDIDATE ONLY. Attached debugger changes observation, not acceptance. */
struct loader_debug_state { int attached,exited,initial_breakpoint_seen,clean; unsigned int events,strings,loads; char last_module[49]; };
/* Diagnostic only: sanitized basename and directory class of a loaded image.
 * The full path never leaves memory. Changes vs loader1797 as supplied:
 *  (1) records the last successfully loaded module in the debug state, so the
 *      EXIT_PROCESS summary can name it; the denied module itself never fires
 *      LOAD_DLL, so it is identified as the first expected import (dumpbin
 *      /dependents of python.exe and python314.dll, run in CI) that is absent
 *      from the observed load sequence -- that inference stays INFERRED.
 *  (2) size_t-clean comparisons so the file still compiles under MSVC /W4 /WX
 *      (loader1797 was only ever built with clang/mingw).
 *  (3) directory class also matches a DOS path without the \\?\ prefix. */
static void loader_debug_module(HANDLE file,struct loader_debug_state *s){
 wchar_t path[1024]={0};wchar_t sysdir[MAX_PATH]={0};
 if(!file||file==INVALID_HANDLE_VALUE){fputs("loader_debug_module name=none\n",stderr);return;}
 DWORD n=GetFinalPathNameByHandleW(file,path,(DWORD)(sizeof(path)/sizeof(path[0])),FILE_NAME_NORMALIZED|VOLUME_NAME_DOS);
 if(!n||(size_t)n>=sizeof(path)/sizeof(path[0])){fprintf(stderr,"loader_debug_module name=unavailable error=%lu\n",(unsigned long)(n?n:GetLastError()));return;}
 const wchar_t *base=path;for(const wchar_t *c=path;*c;c++)if(*c==L'\\')base=c+1;
 char safe[49]={0};size_t count=0;
 for(const wchar_t *c=base;*c && count<48;c++){
  wchar_t ch=*c;int ok=(ch>=L'a'&&ch<=L'z')||(ch>=L'A'&&ch<=L'Z')||(ch>=L'0'&&ch<=L'9')||ch==L'.'||ch==L'_'||ch==L'-';
  safe[count++]=ok?(char)ch:'?';
 }
 int clamped=wcslen(base)>(size_t)48;
 const wchar_t *volpath=wcsncmp(path,L"\\\\?\\",4)==0?path+4:path;
 const char *where="other";
 UINT sl=GetSystemDirectoryW(sysdir,MAX_PATH);
 if(sl && sl<MAX_PATH && _wcsnicmp(volpath,sysdir,sl)==0 && volpath[sl]==L'\\' && base==volpath+sl+1)where="system32";
 fprintf(stderr,"loader_debug_module name=%s clamped=%d dir=%s\n",safe,clamped,where);
 if(s){memcpy(s->last_module,safe,sizeof(s->last_module));s->last_module[sizeof(s->last_module)-1]=0;s->loads++;}
 SecureZeroMemory(path,sizeof(path));
}
static void loader_debug_string(HANDLE process,const OUTPUT_DEBUG_STRING_INFO *info){
 BYTE data[8192]={0};SIZE_T received=0;char safe[4097]={0};
 SIZE_T size=info->nDebugStringLength;if(size>sizeof(data)-2)size=sizeof(data)-2;
 if(!size || !ReadProcessMemory(process,info->lpDebugStringData,data,size,&received) || received!=size){fprintf(stderr,"loader_debug_string readable=0 declared_bytes=%lu received_bytes=%lu unicode=%d no_localization_claim=1\n",(unsigned long)info->nDebugStringLength,(unsigned long)received,info->fUnicode!=0);SecureZeroMemory(data,sizeof(data));return;}
 /* Only fixed labels and numeric statuses leave memory. No path/raw text logging. */
 size_t count=0;
 if(info->fUnicode){for(size_t i=0;i+1<size && count<4096;i+=2)safe[count++]=data[i+1]==0 && data[i]<128?(char)data[i]:'?';}
 else {for(size_t i=0;i<size && count<4096;i++)safe[count++]=data[i]<128?(char)data[i]:'?';}
 const char *names[]={"python.exe","python314.dll","vcruntime140.dll","vcruntime140_1.dll","ntdll.dll","kernel32.dll","kernelbase.dll","ucrtbase.dll","version.dll","ws2_32.dll","bcrypt.dll","bcryptprimitives.dll","advapi32.dll","sechost.dll","rpcrt4.dll"};
 unsigned long bits=0;for(size_t i=0;i<sizeof(names)/sizeof(names[0]);i++)if(strstr(safe,names[i]))bits|=1UL<<i;
 fprintf(stderr,"loader_debug_string readable=1 fixed_image_bits=%lu bounded_bytes=%lu unicode=%d clamped=%d raw_discarded=1",bits,(unsigned long)received,info->fUnicode!=0,size!=info->nDebugStringLength);
 /* Fixed-vocabulary keyword bits only; no raw text leaves memory. Lets the
  * next log say whether the single loader string was a failure report at
  * all, which loader1797 could not. */
 const char *words[]={"Ldr","failed","Failed","denied","Denied","access","Access","error","Error","status","Status","cannot","Cannot"};
 unsigned long wbits=0;for(size_t i=0;i<sizeof(words)/sizeof(words[0]);i++)if(strstr(safe,words[i]))wbits|=1UL<<i;
 fprintf(stderr," keyword_bits=%lu ldr_prefix=%d",wbits,strncmp(safe,"LDR:",4)==0);
 /* No debug-string text is ever printed: only the fixed-vocabulary bit masks above. */
 unsigned int emitted=0;for(size_t i=0;i+10<=count && emitted<4;i++)if(safe[i]=='0' && (safe[i+1]=='x'||safe[i+1]=='X')){
  unsigned long value=0;int valid=1;for(size_t j=0;j<8;j++){char ch=safe[i+2+j];unsigned int digit;if(ch>='0'&&ch<='9')digit=(unsigned int)(ch-'0');else if(ch>='a'&&ch<='f')digit=(unsigned int)(ch-'a'+10);else if(ch>='A'&&ch<='F')digit=(unsigned int)(ch-'A'+10);else {valid=0;break;}value=(value<<4)|digit;}
  if(valid && (value&0xFFFF0000UL)==0xC0000000UL){fprintf(stderr," ntstatus_%u=%08lx",emitted,value);emitted++;i+=9;}
 }
 fputc('\n',stderr);SecureZeroMemory(data,sizeof(data));SecureZeroMemory(safe,sizeof(safe));
}
static int loader_debug_poll(struct loader_debug_state *s,HANDLE process,DWORD pid){
 DEBUG_EVENT event={0};SetLastError(0);
 if(!WaitForDebugEventEx(&event,0)){DWORD error=GetLastError();if(error==ERROR_SEM_TIMEOUT)return 2;fprintf(stderr,"loader_debug_wait error=%lu\n",(unsigned long)error);return 0;}
 int boundary_ok=(event.dwProcessId==pid && ++s->events<=1024);
 if(!boundary_ok){
  if(event.dwDebugEventCode==CREATE_PROCESS_DEBUG_EVENT && event.u.CreateProcessInfo.hFile)CloseHandle(event.u.CreateProcessInfo.hFile);
  if(event.dwDebugEventCode==LOAD_DLL_DEBUG_EVENT && event.u.LoadDll.hFile)CloseHandle(event.u.LoadDll.hFile);
  if(event.dwDebugEventCode==EXIT_PROCESS_DEBUG_EVENT && event.dwProcessId==pid)s->exited=1;
  DWORD status=event.dwDebugEventCode==EXCEPTION_DEBUG_EVENT?DBG_EXCEPTION_NOT_HANDLED:DBG_CONTINUE;BOOL continued=ContinueDebugEvent(event.dwProcessId,event.dwThreadId,status);fprintf(stderr,"loader_debug_boundary_refusal event_continued=%d cleanup_detach_expected=1\n",continued);return 0;
 }
 DWORD continuation=DBG_CONTINUE;int ok=1;
 fprintf(stderr,"loader_debug_event kind=%lu ordinal=%u\n",(unsigned long)event.dwDebugEventCode,s->events);
 switch(event.dwDebugEventCode){
 /* Windows closes event process/thread handles at continued EXIT events.
  * Only hFile is owned for manual close. pi handles are separate and caller-owned. */
 case CREATE_PROCESS_DEBUG_EVENT:
  if(event.u.CreateProcessInfo.hFile&&!CloseHandle(event.u.CreateProcessInfo.hFile))ok=0;
  break;
 case LOAD_DLL_DEBUG_EVENT:
  loader_debug_module(event.u.LoadDll.hFile,s);
  if(event.u.LoadDll.hFile&&!CloseHandle(event.u.LoadDll.hFile))ok=0;
  break;
 case OUTPUT_DEBUG_STRING_EVENT:
  if(++s->strings>256)ok=0;else loader_debug_string(process,&event.u.DebugString);
  break;
 case EXCEPTION_DEBUG_EVENT:{
  DWORD code=event.u.Exception.ExceptionRecord.ExceptionCode;
  fprintf(stderr,"loader_debug_exception code=%08lx first_chance=%lu\n",(unsigned long)code,(unsigned long)event.u.Exception.dwFirstChance);
  if(code==EXCEPTION_BREAKPOINT && event.u.Exception.dwFirstChance && !s->initial_breakpoint_seen)s->initial_breakpoint_seen=1;
  else continuation=DBG_EXCEPTION_NOT_HANDLED;
  break;
 }
 case EXIT_PROCESS_DEBUG_EVENT:
  fprintf(stderr,"loader_debug_summary loads=%u last_module=%s\n",s->loads,s->loads?s->last_module:"none");
  fprintf(stderr,"loader_debug_exit code=%08lx\n",(unsigned long)event.u.ExitProcess.dwExitCode);s->exited=1;break;
 default:break;
 }
 if(!ContinueDebugEvent(event.dwProcessId,event.dwThreadId,continuation))ok=0;
 return ok;
}
