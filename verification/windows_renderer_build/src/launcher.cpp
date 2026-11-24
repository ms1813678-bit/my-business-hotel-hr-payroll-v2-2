// wv2launcher: runs at the user's normal (Medium) token, OUTSIDE the box. SOURCE ONLY: not compiled, not run on Windows.
// Local only. Starts ONE arm: creates profile, generated fixture, suspended host in a kill-on-close job, inspects it
// BEFORE it runs, resumes, sweeps, writes typed evidence, then terminates, waits and removes everything it created.
// It makes NO ACL/firewall/loopback change and has no provisioning mode. Host exe and runtime dir must already be readable by the
// AppContainer; that is a separate decision for someone with authority and is not done here.
#include <windows.h>
#include <userenv.h>
#include <sddl.h>
#include <tlhelp32.h>
#include <winsock2.h>
#include <ws2tcpip.h>
#include <iphlpapi.h>
#include <softpub.h>
#include <wintrust.h>
#include <appmodel.h>
#include <winternl.h>
#include <bcrypt.h>
#include <filesystem>
#include <string>
#include <vector>
#include <set>
#include <fstream>
#include <sstream>
#include <thread>
#include <atomic>
#include <mutex>
#include "common.h"
#include "raii.h"
#pragma comment(lib,"userenv.lib")
#pragma comment(lib,"wintrust.lib")
#pragma comment(lib,"ntdll.lib")
#pragma comment(lib,"bcrypt.lib")
#pragma comment(lib,"ws2_32.lib")
namespace fs=std::filesystem;
using std::wstring;

// ---------- RAII: unique, move-only (see raii.h). Every owner is closed by a CHECKED Close() in the final cleanup; the destructor is only a backstop and reports through Err. ----------
struct HandleTr { using type=HANDLE; static type null(){ return nullptr; } static type norm(type h){ return h==INVALID_HANDLE_VALUE?nullptr:h; }
  static bool close(type h){ return CloseHandle(h)!=0; } static long err(){ return (long)GetLastError(); } static constexpr const char* name="CloseHandle"; };
struct FreeSidTr { using type=PSID; static type null(){ return nullptr; } static type norm(type p){ return p; }
  static bool close(type p){ return FreeSid(p)==nullptr; } static long err(){ return -1; } static constexpr const char* name="FreeSid"; };       // FreeSid returns NULL on success (docs)
struct LocalSidTr { using type=PSID; static type null(){ return nullptr; } static type norm(type p){ return p; }
  static bool close(type p){ return LocalFree(p)==nullptr; } static long err(){ return (long)GetLastError(); } static constexpr const char* name="LocalFree"; }; // LocalFree returns NULL on success (docs)
using H=Unique<HandleTr>; using SidBox=Unique<FreeSidTr>; using LocalSid=Unique<LocalSidTr>;
static_assert(!std::is_copy_constructible_v<H>&&!std::is_copy_assignable_v<H>&&std::is_nothrow_move_constructible_v<H>&&std::is_nothrow_move_assignable_v<H>,"H must be move-only");
static_assert(!std::is_copy_constructible_v<LocalSid>&&std::is_nothrow_move_constructible_v<LocalSid>,"LocalSid must be move-only");
static_assert(!std::is_copy_constructible_v<SidBox>&&std::is_nothrow_move_constructible_v<SidBox>,"SidBox must be move-only");
struct AttrList { std::vector<BYTE> b; LPPROC_THREAD_ATTRIBUTE_LIST l=nullptr; bool init=false; AttrList()=default; AttrList(const AttrList&)=delete; AttrList& operator=(const AttrList&)=delete; ~AttrList(){ if(init) DeleteProcThreadAttributeList(l); } };
struct Wsa { bool ok=false; Wsa(){ WSADATA w; ok=WSAStartup(MAKEWORD(2,2),&w)==0; } Wsa(const Wsa&)=delete; Wsa& operator=(const Wsa&)=delete;
  bool Close(); ~Wsa(){ Close(); } };


struct ApiErr { std::string api; long code; };
static std::vector<ApiErr> g_err; static std::mutex g_mu;
static void Err(const char* api,long code){ std::lock_guard<std::mutex> l(g_mu); g_err.push_back({api,code}); }
#define CHECK_BOOL(call) ([&]{ if(!(call)){ Err(#call,(long)GetLastError()); return false;} return true; }())

static wstring Sid2Str(PSID s){ LPWSTR t=nullptr; wstring r; if(ConvertSidToStringSidW(s,&t)){ r=t; LocalFree(t);} else Err("ConvertSidToStringSidW",(long)GetLastError()); return r; }
static wstring J(const wstring& s){ wstring o; for(wchar_t c:s){ if(c==L'"'||c==L'\\'){ o+=L'\\'; o+=c; } else if(c>=0x20) o+=c; } return o; }

// ---------- nonces (positive-control tokens, generated here, never reused) ----------
static std::vector<std::pair<wstring,wstring>> MakeNonces(){
  std::vector<std::pair<wstring,wstring>> v;
  for(auto id:kAttemptIds){ BYTE r[16]; if(BCryptGenRandom(nullptr,r,sizeof r,BCRYPT_USE_SYSTEM_PREFERRED_RNG)!=0){ Err("BCryptGenRandom",-1); return {}; }
    wchar_t hex[33]; for(int i=0;i<16;i++) swprintf(hex+2*i,3,L"%02x",r[i]); v.push_back({id,hex}); }
  return v; }

bool Wsa::Close(){ if(!ok) return true; ok=false; if(WSACleanup()!=0){ Err("WSACleanup",WSAGetLastError()); return false; } return true; }
static bool g_cleanupOk=true;   // set false by any failed teardown step; reported in the evidence AND in the exit code
static void CleanFail(const char* api,long code){ Err(api,code); g_cleanupOk=false; }

static std::string U8(const wstring& s){ if(s.empty()) return {}; int n=WideCharToMultiByte(CP_UTF8,0,s.c_str(),(int)s.size(),0,0,0,0); std::string u(n,0); WideCharToMultiByte(CP_UTF8,0,s.c_str(),(int)s.size(),u.data(),n,0,0); return u; }
// exclusive create: CREATE_NEW fails if the path already exists (never overwrites, never truncates)
static bool WriteNew(const fs::path& path,const std::string& bytes){
  H f(CreateFileW(path.c_str(),GENERIC_WRITE,0,nullptr,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,nullptr)); if(!f){ Err("CreateFileW CREATE_NEW",(long)GetLastError()); return false; }
  size_t off=0; while(off<bytes.size()){ DWORD w=0; if(!WriteFile(f.get(),bytes.data()+off,(DWORD)(bytes.size()-off),&w,nullptr)||w==0){ Err("WriteFile",(long)GetLastError()); return false; } off+=w; }
  return f.Close(); }
// exclusive directory create: CreateDirectoryW fails with ERROR_ALREADY_EXISTS if it exists; that is a rejection, not reuse
static bool MakeDirNew(const fs::path& p,const char* what){ if(!CreateDirectoryW(p.c_str(),nullptr)){ Err(what,(long)GetLastError()); return false; } return true; }
static wstring RandHex(int bytes){ std::vector<BYTE> r(bytes); if(BCryptGenRandom(nullptr,r.data(),(ULONG)r.size(),BCRYPT_USE_SYSTEM_PREFERRED_RNG)!=0){ Err("BCryptGenRandom",-1); return L""; }
  wstring s; for(BYTE b:r){ wchar_t h[3]; swprintf(h,3,L"%02x",b); s+=h; } return s; }

// ---------- profile: unique random name, exclusive create. An existing profile is REJECTED (never derived, never reused, never deleted). Deleted at the end only if this run created it. ----------
struct Profile { wstring name; SidBox sid; bool created=false; std::vector<LocalSid> capKeep; std::vector<SID_AND_ATTRIBUTES> caps; };
static bool EnsureProfile(Profile& p,const std::vector<wstring>& capSids){
  p.capKeep.reserve(capSids.size()); p.caps.reserve(capSids.size());
  for(auto& c:capSids){ LocalSid s; if(!CHECK_BOOL(ConvertStringSidToSidW(c.c_str(),s.put()))) return false; p.caps.push_back({s.get(),SE_GROUP_ENABLED}); p.capKeep.push_back(std::move(s)); }   // move: pointer ownership transfers, s is zeroed
  HRESULT hr=CreateAppContainerProfile(p.name.c_str(),p.name.c_str(),L"Hotel HR WebView2 compatibility test",p.caps.empty()?nullptr:p.caps.data(),(DWORD)p.caps.size(),p.sid.put());
  if(hr==HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS)){ Err("CreateAppContainerProfile: name already exists, rejected",(long)hr); return false; }
  if(FAILED(hr)){ Err("CreateAppContainerProfile",(long)hr); return false; }
  p.created=true; return true; }

// NetworkIsolationGetAppContainerConfig: DWORD(DWORD*,PSID_AND_ATTRIBUTES*), FirewallAPI.dll, documented sample loads it with GetProcAddress and frees with HeapFree
// for every returned SID and then for the array (learn.microsoft.com .../netfw/nf-netfw-networkisolationgetappcontainerconfig). Read from that page, not compiled.
using FnNIGetCfg=DWORD (WINAPI*)(DWORD*,PSID_AND_ATTRIBUTES*);
static wstring LoopbackState(PSID ac){
  HMODULE m=LoadLibraryExW(L"FirewallAPI.dll",nullptr,LOAD_LIBRARY_SEARCH_SYSTEM32); if(!m){ Err("LoadLibraryExW FirewallAPI.dll",(long)GetLastError()); return L"unknown"; }
  wstring r=L"unknown"; auto fn=reinterpret_cast<FnNIGetCfg>(GetProcAddress(m,"NetworkIsolationGetAppContainerConfig"));
  if(!fn) Err("GetProcAddress NetworkIsolationGetAppContainerConfig",(long)GetLastError());
  else { DWORD cnt=0; PSID_AND_ATTRIBUTES arr=nullptr; DWORD rc=fn(&cnt,&arr);
    if(rc!=ERROR_SUCCESS) Err("NetworkIsolationGetAppContainerConfig",(long)rc);
    else { r=L"absent"; for(DWORD i=0;i<cnt;i++) if(EqualSid(arr[i].Sid,ac)) r=L"PRESENT"; }
    if(arr){ for(DWORD i=0;i<cnt;i++) if(arr[i].Sid&&!HeapFree(GetProcessHeap(),0,arr[i].Sid)) Err("HeapFree sid",(long)GetLastError()); if(!HeapFree(GetProcessHeap(),0,arr)) Err("HeapFree array",(long)GetLastError()); } }
  if(!FreeLibrary(m)) Err("FreeLibrary",(long)GetLastError());
  return r; }

static bool WaitJobEmpty(HANDLE job,DWORD ms){ ULONGLONG end=GetTickCount64()+ms; for(;;){ JOBOBJECT_BASIC_ACCOUNTING_INFORMATION a{};
  if(!QueryInformationJobObject(job,JobObjectBasicAccountingInformation,&a,sizeof a,nullptr)){ Err("QueryInformationJobObject accounting",(long)GetLastError()); return false; }
  if(a.ActiveProcesses==0) return true; if(GetTickCount64()>=end){ Err("job still has active processes",(long)a.ActiveProcesses); return false; } Sleep(50); } }


// ---------- inspect one process by handle; every failed query becomes an explicit error (never a default value) ----------
static wstring Inspect(HANDLE h,DWORD pid,const wstring& src,HANDLE job){
  std::wstringstream o; o<<L"{\"pid\":"<<pid<<L",\"source\":\""<<src<<L"\"";
  auto bad=[&](const wchar_t* what,long code){ std::wstringstream e; e<<L"{\"pid\":"<<pid<<L",\"error\":\""<<what<<L" "<<code<<L"\"}"; return e.str(); };
  BOOL inJob=FALSE; if(!IsProcessInJob(h,job,&inJob)) return bad(L"IsProcessInJob",(long)GetLastError()); o<<L",\"in_job\":"<<(inJob?L"true":L"false");
  wchar_t img[MAX_PATH*2]; DWORD n=MAX_PATH*2; if(!QueryFullProcessImageNameW(h,0,img,&n)) return bad(L"QueryFullProcessImageName",(long)GetLastError()); o<<L",\"image\":\""<<J(img)<<L"\"";
  // ProcessCommandLineInformation (60) is not in winternl.h: UNVERIFIED against the SDK. A failure is an explicit error, not an empty string.
  ULONG need=0; NtQueryInformationProcess(h,(PROCESSINFOCLASS)60,nullptr,0,&need); if(!need) return bad(L"cmdline-size",0);
  { std::vector<BYTE> b(need); NTSTATUS st=NtQueryInformationProcess(h,(PROCESSINFOCLASS)60,b.data(),need,&need); if(!NT_SUCCESS(st)) return bad(L"cmdline NTSTATUS",(long)st);
    auto* us=(UNICODE_STRING*)b.data(); o<<L",\"cmdline\":\""<<J(wstring(us->Buffer,us->Length/2))<<L"\""; }
  UINT32 pl=0; LONG pr=GetPackageFamilyName(h,&pl,nullptr); o<<L",\"package_query\":"<<pr;
  HANDLE t0; if(!OpenProcessToken(h,TOKEN_QUERY,&t0)) return bad(L"OpenProcessToken",(long)GetLastError()); H t(t0);
  DWORD ac=0,l=0; if(!GetTokenInformation(t.get(),TokenIsAppContainer,&ac,sizeof ac,&l)) return bad(L"TokenIsAppContainer",(long)GetLastError());
  o<<L",\"is_app_container\":"<<(ac?L"true":L"false");
  if(ac){ DWORD len=0; GetTokenInformation(t.get(),TokenAppContainerSid,nullptr,0,&len); if(!len) return bad(L"TokenAppContainerSid size",(long)GetLastError()); std::vector<BYTE> b(len);
    if(!GetTokenInformation(t.get(),TokenAppContainerSid,b.data(),len,&len)) return bad(L"TokenAppContainerSid",(long)GetLastError());
    o<<L",\"ac_sid\":\""<<Sid2Str(((TOKEN_APPCONTAINER_INFORMATION*)b.data())->TokenAppContainer)<<L"\"";
    len=0; GetTokenInformation(t.get(),TokenCapabilities,nullptr,0,&len); DWORD e=GetLastError(); if(!len && e!=ERROR_INSUFFICIENT_BUFFER) return bad(L"TokenCapabilities size",(long)e);
    std::vector<BYTE> c(len?len:sizeof(TOKEN_GROUPS)); if(!GetTokenInformation(t.get(),TokenCapabilities,c.data(),(DWORD)c.size(),&len)) return bad(L"TokenCapabilities",(long)GetLastError());
    auto* g=(TOKEN_GROUPS*)c.data(); o<<L",\"capabilities\":["; for(DWORD i=0;i<g->GroupCount;i++) o<<(i?L",":L"")<<L"\""<<Sid2Str(g->Groups[i].Sid)<<L"\""; o<<L"]"; }
  { DWORD len=0; GetTokenInformation(t.get(),TokenIntegrityLevel,nullptr,0,&len); if(!len) return bad(L"TokenIntegrityLevel size",(long)GetLastError()); std::vector<BYTE> il(len);
    if(!GetTokenInformation(t.get(),TokenIntegrityLevel,il.data(),len,&len)) return bad(L"TokenIntegrityLevel",(long)GetLastError());
    auto* m=(TOKEN_MANDATORY_LABEL*)il.data(); o<<L",\"integrity_rid\":"<<*GetSidSubAuthority(m->Label.Sid,*GetSidSubAuthorityCount(m->Label.Sid)-1); }
  // Authenticode: cache-only retrieval and no revocation check are REQUESTED via the documented flags, so WinVerifyTrust should not use the network.
  // signature_offline=true records only that those flags were set; whether the call really stayed offline was NOT observed.
  WINTRUST_FILE_INFO fi{}; fi.cbStruct=sizeof fi; fi.pcwszFilePath=img; WINTRUST_DATA wd{}; wd.cbStruct=sizeof wd; wd.dwUIChoice=WTD_UI_NONE; wd.dwUnionChoice=WTD_CHOICE_FILE; wd.pFile=&fi;
  wd.dwStateAction=WTD_STATEACTION_VERIFY; wd.fdwRevocationChecks=WTD_REVOKE_NONE; wd.dwProvFlags=WTD_CACHE_ONLY_URL_RETRIEVAL|WTD_REVOCATION_CHECK_NONE;
  GUID a=WINTRUST_ACTION_GENERIC_VERIFY_V2; LONG sr=WinVerifyTrust(nullptr,&a,&wd); wd.dwStateAction=WTD_STATEACTION_CLOSE; WinVerifyTrust(nullptr,&a,&wd);
  o<<L",\"signature_status\":"<<sr<<L",\"signature_offline\":true}"; return o.str(); }
static bool IsViolation(const wstring& r){ return r.find(L"\"error\"")!=wstring::npos || r.find(L"\"is_app_container\":true")==wstring::npos || r.find(L"S-1-15-3-")!=wstring::npos || r.find(L"\"capabilities\":[]")==wstring::npos; }

static std::set<DWORD> JobPids(HANDLE job){ std::set<DWORD> r; std::vector<BYTE> b(sizeof(JOBOBJECT_BASIC_PROCESS_ID_LIST)+4096*sizeof(ULONG_PTR)); auto* l=(JOBOBJECT_BASIC_PROCESS_ID_LIST*)b.data();
  if(!QueryInformationJobObject(job,JobObjectBasicProcessIdList,l,(DWORD)b.size(),nullptr)){ Err("QueryInformationJobObject",(long)GetLastError()); return r; }
  for(DWORD i=0;i<l->NumberOfProcessIdsInList;i++) r.insert((DWORD)l->ProcessIdList[i]); return r; }
static std::set<DWORD> Descendants(DWORD root){ std::set<DWORD> r{root}; H s(CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS,0)); if(!s){ Err("CreateToolhelp32Snapshot",(long)GetLastError()); return r; }
  PROCESSENTRY32W e{}; e.dwSize=sizeof e; std::vector<std::pair<DWORD,DWORD>> v; if(Process32FirstW(s.get(),&e)) do v.push_back({e.th32ProcessID,e.th32ParentProcessID}); while(Process32NextW(s.get(),&e));
  for(bool ch=true;ch;){ ch=false; for(auto&p:v) if(r.count(p.second)&&!r.count(p.first)){ r.insert(p.first); ch=true; } } return r; }

// ---------- loopback listener, outside the box. Records first request line (carries the nonce). Stop is checked every accept/recv timeout. ----------
static std::atomic<bool> g_stop; static std::wstring g_hits; static std::atomic<int> g_listenOk{0};
static void Listen(){ SOCKET s=socket(AF_INET,SOCK_STREAM,0); if(s==INVALID_SOCKET){ Err("listener socket",WSAGetLastError()); g_listenOk=-1; return; }
  sockaddr_in a{}; a.sin_family=AF_INET; a.sin_port=htons(kLoopPort); inet_pton(AF_INET,"127.0.0.1",&a.sin_addr);
  if(bind(s,(sockaddr*)&a,sizeof a)||listen(s,16)){ Err("listener bind/listen",WSAGetLastError()); closesocket(s); g_listenOk=-1; return; }
  u_long nb=1; ioctlsocket(s,FIONBIO,&nb); g_listenOk=1;
  while(!g_stop){ SOCKET c=accept(s,nullptr,nullptr); if(c==INVALID_SOCKET){ Sleep(50); continue; }
    DWORD tmo=1000; setsockopt(c,SOL_SOCKET,SO_RCVTIMEO,(char*)&tmo,sizeof tmo); char buf[512]={}; int n=recv(c,buf,sizeof buf-1,0); std::string line(buf,n>0?n:0); line=line.substr(0,line.find('\r')); 
    std::wstring w(line.begin(),line.end()); g_hits+=(g_hits.empty()?L"":L",")+std::wstring(L"{\"first_line\":\"")+J(w)+L"\"}"; if(closesocket(c)==SOCKET_ERROR) Err("listener closesocket(client)",WSAGetLastError()); }
  if(closesocket(s)==SOCKET_ERROR) Err("listener closesocket",WSAGetLastError()); }

static const wchar_t* kProbePage=LR"(<!doctype html><meta charset=utf-8><title>pending</title><script>
// GENERATED FIXTURE (test only). Reports once through document.title: "R|id=outcome;..." with outcomes connected|error|timeout. No bridge.
const q=new URLSearchParams(location.search),r={};
const tmo=(ms)=>new Promise(x=>setTimeout(()=>x('timeout'),ms));
const f=async(id,u)=>{try{r[id]=await Promise.race([fetch(u,{mode:'no-cors',cache:'no-store'}).then(()=>'connected'),tmo(5000)])}catch(e){r[id]='error'}};
const w=(id,u)=>new Promise(res=>{try{const s=new WebSocket(u);s.onopen=()=>{r[id]='connected';s.close();res()};s.onerror=()=>{r[id]=r[id]||'error';res()};setTimeout(()=>{r[id]=r[id]||'timeout';res()},5000)}catch(e){r[id]='error';res()}});
(async()=>{
  await f('rend_http_loopback','http://'+q.get('loop')+'/p?n='+q.get('n_rend_http_loopback'));
  await f('rend_http_remote','http://'+q.get('remote')+'/p?n='+q.get('n_rend_http_remote'));
  await w('rend_ws_remote','ws://'+q.get('remote')+'/p?n='+q.get('n_rend_ws_remote'));
  document.title='R|'+Object.entries(r).map(([k,v])=>k+'='+v).join(';');
})();
</script>)";

static int Run(const wstring& arm,const wstring& host,const wstring& runtime,const wstring& remote,const wstring& outDir){
  if(arm!=L"A"&&arm!=L"B"&&arm!=L"C"){ return 2; }
  g_closeFailHook=[](const char* a,long c){ Err(a,c); };      // destructor-path close failures are recorded too
  Wsa wsa; if(!wsa.ok) Err("WSAStartup",-1);
  auto nonces=MakeNonces();
  Profile prof; bool contained=arm!=L"C";
  bool ok=wsa.ok && !nonces.empty();
  wstring sidS, pre=L"null", lo=L"unknown"; fs::path runDir, fixture, udf, report; bool runDirCreated=false;
  std::thread lt; g_stop=false;
  H job, proc, thr; DWORD hostPid=0; bool violation=false, resumed=false, listenerStarted=false; std::wstringstream procs; int sweeps=0;
  FILETIME t0{}; GetSystemTimeAsFileTime(&t0);
  AttrList al; SECURITY_CAPABILITIES sc{}; PROCESS_INFORMATION pi{};
  try {
    if(ok&&contained){ wstring rid=RandHex(8); if(rid.empty()) ok=false; else { prof.name=(arm==L"A"?wstring(kAcName):wstring(kAcCtlName))+L"."+rid;
      ok=EnsureProfile(prof, arm==L"B"?std::vector<wstring>{L"S-1-15-3-1",L"S-1-15-3-3"}:std::vector<wstring>{}); } }
    if(ok&&contained) sidS=Sid2Str(prof.sid.get());
    // run root: a fresh, exclusively created directory that THIS run owns and is the only thing it ever deletes.
    // contained arms: inside the new profile's own folder (exists only because this run created the profile). arm C: inside the temp dir.
    fs::path base;
    if(ok&&contained){ PWSTR f=nullptr; HRESULT hr=GetAppContainerFolderPath(sidS.c_str(),&f); if(FAILED(hr)){ Err("GetAppContainerFolderPath",(long)hr); ok=false; } else { base=f; CoTaskMemFree(f);} }
    if(ok&&!contained){ wchar_t t[MAX_PATH+1]; DWORD n=GetTempPathW(MAX_PATH,t); if(!n||n>MAX_PATH){ Err("GetTempPath",(long)GetLastError()); ok=false; } else base=t; }
    if(ok){ wstring rid=RandHex(8); if(rid.empty()) ok=false; else { runDir=base/(L"wv2run-"+arm+L"-"+rid); ok=MakeDirNew(runDir,"create run dir (exists or failed: rejected)"); runDirCreated=ok; } }
    if(ok){ fixture=runDir/L"fixture"; udf=runDir/L"udf"; report=runDir/L"report.json"; ok=MakeDirNew(fixture,"create fixture dir")&&MakeDirNew(udf,"create udf dir"); }
    if(ok&&!WriteNew(fixture/L"index.html",U8(kProbePage))){ ok=false; }
    if(ok){ lt=std::thread(Listen); listenerStarted=true; for(int i=0;i<100&&!g_listenOk;i++) Sleep(20); if(g_listenOk!=1) ok=false; }
    if(ok){ job=H(CreateJobObjectW(nullptr,nullptr)); if(!job){ Err("CreateJobObjectW",(long)GetLastError()); ok=false; } }
    if(ok){ JOBOBJECT_EXTENDED_LIMIT_INFORMATION jl{}; jl.BasicLimitInformation.LimitFlags=JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; ok=CHECK_BOOL(SetInformationJobObject(job.get(),JobObjectExtendedLimitInformation,&jl,sizeof jl)); }
    HANDLE jobs[1]={job.get()};
    if(ok){ DWORD cnt=contained?2:1; SIZE_T asz=0; InitializeProcThreadAttributeList(nullptr,cnt,0,&asz); if(!asz){ Err("InitializeProcThreadAttributeList size",(long)GetLastError()); ok=false; }
      else { al.b.resize(asz); al.l=(LPPROC_THREAD_ATTRIBUTE_LIST)al.b.data(); ok=CHECK_BOOL(InitializeProcThreadAttributeList(al.l,cnt,0,&asz)); al.init=ok; } }
    if(ok) ok=CHECK_BOOL(UpdateProcThreadAttribute(al.l,0,PROC_THREAD_ATTRIBUTE_JOB_LIST,jobs,sizeof jobs,nullptr,nullptr));
    if(ok&&contained){ sc.AppContainerSid=prof.sid.get(); sc.Capabilities=prof.caps.empty()?nullptr:prof.caps.data(); sc.CapabilityCount=(DWORD)prof.caps.size();
      ok=CHECK_BOOL(UpdateProcThreadAttribute(al.l,0,PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,&sc,sizeof sc,nullptr,nullptr)); }
    // explicit controlled endpoints only; nothing inherited from the ambient environment
    wstring envb; { const wchar_t* keep[]={L"SystemRoot=C:\\Windows",L"windir=C:\\Windows"}; for(auto k:keep){ envb+=k; envb.push_back(L'\0'); } envb.push_back(L'\0'); }
    wstring cmd=L"\""+host+L"\" --arm "+arm+L" --runtime \""+runtime+L"\" --udf \""+udf.wstring()+L"\" --content \""+fixture.wstring()+L"\" --report \""+report.wstring()+L"\" --loop 127.0.0.1:"+std::to_wstring(kLoopPort)+L" --remote "+remote;
    for(auto& n:nonces) cmd+=L" --nonce "+n.first+L"="+n.second;
    if(ok){ STARTUPINFOEXW si{}; si.StartupInfo.cb=sizeof si; si.lpAttributeList=al.l;
      ok=CHECK_BOOL(CreateProcessW(nullptr,cmd.data(),nullptr,nullptr,FALSE,CREATE_SUSPENDED|EXTENDED_STARTUPINFO_PRESENT|CREATE_UNICODE_ENVIRONMENT,envb.data(),runDir.c_str(),&si.StartupInfo,&pi));
      if(ok){ proc=H(pi.hProcess); thr=H(pi.hThread); hostPid=pi.dwProcessId; } }   // handles owned immediately (move-only owners): no leak on later failure
    // inspect while suspended (nothing of the host has run yet)
    if(ok){ pre=Inspect(proc.get(),hostPid,L"suspended",job.get()); if(contained&&IsViolation(pre)){ violation=true; ok=false; } if(pre.find(L"\"error\"")!=wstring::npos) ok=false; }
    if(ok){ if(ResumeThread(thr.get())==(DWORD)-1){ Err("ResumeThread",(long)GetLastError()); ok=false; } else resumed=true; }
    if(ok){ ULONGLONG deadline=GetTickCount64()+60000, reportAt=0;
      while(GetTickCount64()<deadline){ std::set<DWORD> all=JobPids(job.get()); for(DWORD d:Descendants(hostPid)) all.insert(d); auto jp=JobPids(job.get());
        for(DWORD p:all){ H ph(OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,FALSE,p)); wstring r;
          if(!ph){ std::wstringstream e; e<<L"{\"pid\":"<<p<<L",\"error\":\"OpenProcess "<<GetLastError()<<L"\"}"; r=e.str(); } else r=Inspect(ph.get(),p,jp.count(p)?L"job":L"toolhelp",job.get());
          if(contained&&IsViolation(r)&&r.find(L"OpenProcess")==wstring::npos) violation=true; procs<<(procs.tellp()>0?L",":L"")<<r;
          if(ph&&!ph.Close()){ /* recorded by the hook; evidence will carry it */ } }
        sweeps++; if(violation){ if(!TerminateJobObject(job.get(),99)) CleanFail("TerminateJobObject (violation)",(long)GetLastError()); break; }
        std::error_code ec; if(!reportAt && fs::exists(report,ec)) reportAt=GetTickCount64();
        if(ec) Err("exists(report)",ec.value());
        if(reportAt && GetTickCount64()>reportAt+4000) break;      // ~4 s of sweeps after the host report (sweep every 250 ms)
        Sleep(250); } }
    if(contained&&prof.sid.get()) lo=LoopbackState(prof.sid.get());
  } catch(const std::exception&){ Err("exception in run",-2); ok=false; } catch(...){ Err("unknown exception in run",-3); ok=false; }
  FILETIME t1; GetSystemTimeAsFileTime(&t1);
  // ===== final CHECKED cleanup, BEFORE the verdict/evidence is written. Every failure is recorded in api_errors and sets cleanup_ok=false. =====
  if(job){ if(!TerminateJobObject(job.get(),0)) CleanFail("TerminateJobObject",(long)GetLastError()); }
  if(proc){ DWORD w=WaitForSingleObject(proc.get(),10000); if(w!=WAIT_OBJECT_0) CleanFail("WaitForSingleObject host exit",w==WAIT_FAILED?(long)GetLastError():(long)w); }
  if(job&&!WaitJobEmpty(job.get(),10000)) g_cleanupOk=false;
  g_stop=true; if(lt.joinable()) lt.join();
  (void)listenerStarted;
  std::string hostReport; if(runDirCreated){ std::ifstream rf(report,std::ios::binary); if(rf){ std::ostringstream rs; rs<<rf.rdbuf(); hostReport=rs.str(); } if(hostReport.empty()) Err("host report missing or empty",0); }
  if(thr&&!thr.Close()) g_cleanupOk=false; if(proc&&!proc.Close()) g_cleanupOk=false; if(job&&!job.Close()) g_cleanupOk=false;
  if(runDirCreated){ std::error_code ec; fs::remove_all(runDir,ec); if(ec) CleanFail("remove_all run dir",ec.value()); std::error_code ec2; if(fs::exists(runDir,ec2)||ec2) CleanFail("run dir still present after remove_all",ec2.value()); }
  if(contained&&prof.created){ HRESULT hr=DeleteAppContainerProfile(prof.name.c_str()); if(FAILED(hr)) CleanFail("DeleteAppContainerProfile",(long)hr); else prof.created=false; }
  if(!prof.sid.Close()) g_cleanupOk=false; for(auto& c:prof.capKeep) if(!c.Close()) g_cleanupOk=false;
  if(!wsa.Close()) g_cleanupOk=false;
  // evidence is built only now, so api_errors and cleanup_ok include every cleanup result above
  std::wstringstream out; out<<L"{\"arm\":\""<<arm<<L"\",\"expected_ac_sid\":\""<<sidS<<L"\",\"violation_tripwire\":"<<(violation?L"true":L"false")<<L",\"loopback_exemption\":\""<<lo<<L"\",\"host_pid\":"<<hostPid
      <<L",\"start_filetime\":"<<(((ULONGLONG)t0.dwHighDateTime<<32)|t0.dwLowDateTime)<<L",\"end_filetime\":"<<(((ULONGLONG)t1.dwHighDateTime<<32)|t1.dwLowDateTime)
      <<L",\"sweeps\":"<<sweeps<<L",\"resumed_after_inspection\":"<<(resumed?L"true":L"false")<<L",\"cleanup_ok\":"<<(g_cleanupOk?L"true":L"false")<<L",\"pre_resume\":"<<pre<<L",\"nonces\":{";
  for(size_t i=0;i<nonces.size();i++) out<<(i?L",":L"")<<L"\""<<nonces[i].first<<L"\":\""<<nonces[i].second<<L"\"";
  out<<L"},\"processes\":["<<procs.str()<<L"],\"listener_hits\":["<<g_hits<<L"],\"api_errors\":[";
  { std::lock_guard<std::mutex> l(g_mu); for(size_t i=0;i<g_err.size();i++) out<<(i?L",":L"")<<L"{\"api\":\""<<J(wstring(g_err[i].api.begin(),g_err[i].api.end()))<<L"\",\"code\":"<<g_err[i].code<<L"}"; }
  out<<L"]}";
  // the evidence files are the one thing written after the cleanup; their own failure cannot be inside them, so it is an exit code + stderr
  bool w1=WriteNew(fs::path(outDir)/(L"arm-"+arm+L".json"),U8(out.str()));
  bool w2=WriteNew(fs::path(outDir)/(L"host-report-"+arm+L".json"),hostReport);
  if(!w1||!w2){ fprintf(stderr,"evidence write failed (existing file is refused, never overwritten)\n"); return 41; }
  if(!g_cleanupOk){ fprintf(stderr,"cleanup failed: see api_errors / cleanup_ok in the evidence; verdict cannot be COMPAT_OBSERVED\n"); return 42; }
  return violation?99:(ok?0:40); }

int wmain(int c,wchar_t** v){
  auto A=[&](const wchar_t* k){ for(int i=1;i+1<c;i++) if(!wcscmp(v[i],k)) return wstring(v[i+1]); return wstring(); };
  if(c>1&&!wcscmp(v[1],L"--run")){ wstring arm=A(L"--arm"),host=A(L"--host"),rt=A(L"--runtime"),rem=A(L"--remote"),out=A(L"--out");
    if(arm.empty()||host.empty()||rt.empty()||rem.empty()||out.empty()) { wprintf(L"missing argument\n"); return 2; } return Run(arm,host,rt,rem,out); }
  wprintf(L"usage: --run --arm A|B|C --host exe --runtime dir --remote <ipv4>:<port> --out dir\nNo provisioning mode. No ACL, firewall or loopback change is ever made by this tool.\n"); return 2; }
