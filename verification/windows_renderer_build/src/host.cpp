// wv2host: native WebView2 host for the AppContainer COMPATIBILITY test (observed compatibility, not a security confirmation).
// Status: SOURCE ONLY. Not compiled (no MSVC/MinGW toolchain on the authoring box). Not run.
#include <windows.h>
#include <shlwapi.h>
#include <shlobj.h>
#include <sddl.h>
#include <winsock2.h>
#include <ws2tcpip.h>
#include <wrl.h>
#include <string>
#include <vector>
#include <sstream>
#include <fstream>
#include <algorithm>
#include "WebView2.h"
#include "WebView2EnvironmentOptions.h"
#include "common.h"
using Microsoft::WRL::Callback;
using Microsoft::WRL::ComPtr;

namespace {
struct Cfg {
  std::wstring runtime, udf, content, report, arm = L"A", remote /*ipv4:port, controlled*/, loop /*127.0.0.1:port*/;
  std::vector<std::pair<std::wstring,std::wstring>> nonces;   // attempt id -> launcher-generated nonce
} g;
struct Attempt { std::wstring id, outcome=L"not_attempted"; int wsa=0, so=-1; };
std::vector<Attempt> g_att; std::vector<std::pair<std::wstring,long>> g_apiErr;
void ApiErr(const wchar_t* api,long code){ g_apiErr.push_back({api,code}); }
#define HR_STR2(x) L##x
#define HR_STR(x) HR_STR2(#x)
#define HR(x) do{ HRESULT hr_=(x); if(FAILED(hr_)) ApiErr(HR_STR(x),(long)hr_); }while(0)
std::wstring Nonce(const std::wstring& id){ for(auto&n:g.nonces) if(n.first==id) return n.second; return L""; }
// one owned wstring copy, converted once to a constant narrow string (launcher nonces are lowercase hex; anything else is refused, never guessed)
std::string NonceAscii(const std::wstring& id){ const std::wstring w=Nonce(id); std::string s; s.reserve(w.size());
  for(wchar_t c: w){ if(!((c>=L'0'&&c<=L'9')||(c>=L'a'&&c<=L'f'))) return std::string(); s.push_back(static_cast<char>(c)); } return s; }
Attempt* Att(const std::wstring& id){ for(auto&a:g_att) if(a.id==id) return &a; return nullptr; }
HWND g_hwnd; ComPtr<ICoreWebView2Environment> g_env; ComPtr<ICoreWebView2Controller> g_ctl; ComPtr<ICoreWebView2> g_wv;
std::wstring g_log; HRESULT g_envHr = E_PENDING, g_ctlHr = E_PENDING; std::wstring g_title; bool g_reported=false;
void Log(const std::wstring& s){ g_log += s + L"\n"; }
std::wstring J(const std::wstring& s){ std::wstring o; for(wchar_t c: s){ if(c==L'"'||c==L'\\') o+=L'\\'; if(c<0x20) continue; o+=c;} return o; }

bool SelfIsAppContainer(std::wstring* sid){
  HANDLE t; if(!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &t)) return false;
  DWORD ac=0, n=0; bool is = GetTokenInformation(t, TokenIsAppContainer, &ac, sizeof ac, &n) && ac;
  if(is && sid){ DWORD len=0; GetTokenInformation(t, TokenAppContainerSid, nullptr, 0, &len);
    std::vector<BYTE> b(len); auto* ta=(TOKEN_APPCONTAINER_INFORMATION*)b.data();
    if(GetTokenInformation(t, TokenAppContainerSid, ta, len, &len)){ LPWSTR s=nullptr; if(ConvertSidToStringSidW(ta->TokenAppContainer,&s)){ *sid=s; LocalFree(s);} } }
  CloseHandle(t); return is; }

// One TCP connect attempt to an IPv4 LITERAL (no DNS at all). Outcome is typed; only SO_ERROR/WSA codes read at the right moment are used.
//   connected: connect()==0, or writable AND SO_ERROR==0 AND getpeername succeeds. denied: WSAEACCES (10013). refused: 10061. timeout: select()==0.
// A nonce request line is sent after connect so the listener can prove arrival.
void TryConnect(const std::wstring& id,const std::wstring& hostport){
  Attempt a; a.id=id; auto p=hostport.rfind(L':');
  if(p==std::wstring::npos){ a.outcome=L"not_attempted"; ApiErr(L"bad-target",0); g_att.push_back(a); return; }
  sockaddr_in sa{}; sa.sin_family=AF_INET; int port=_wtoi(hostport.substr(p+1).c_str()); sa.sin_port=htons((u_short)port);
  if(port<=0||port>65535||InetPtonW(AF_INET,hostport.substr(0,p).c_str(),&sa.sin_addr)!=1){ a.outcome=L"not_attempted"; ApiErr(L"InetPtonW/port",0); g_att.push_back(a); return; }
  SOCKET s=socket(AF_INET,SOCK_STREAM,IPPROTO_TCP); if(s==INVALID_SOCKET){ a.outcome=L"other_error"; a.wsa=WSAGetLastError(); ApiErr(L"socket",a.wsa); g_att.push_back(a); return; }
  u_long nb=1; if(ioctlsocket(s,FIONBIO,&nb)!=0){ a.outcome=L"other_error"; a.wsa=WSAGetLastError(); ApiErr(L"ioctlsocket",a.wsa); closesocket(s); g_att.push_back(a); return; }
  int rc=connect(s,(sockaddr*)&sa,sizeof sa); int e= rc==0?0:WSAGetLastError();   // WSA error read ONLY when connect failed
  auto finish=[&](const wchar_t* o,int so){ a.outcome=o; a.so=so; };
  if(rc==0){ finish(L"connected",0); }
  else if(e!=WSAEWOULDBLOCK){ a.wsa=e; finish(e==WSAEACCES?L"denied":(e==WSAECONNREFUSED?L"refused":L"other_error"),e); }
  else { fd_set w,x; FD_ZERO(&w);FD_ZERO(&x);FD_SET(s,&w);FD_SET(s,&x); timeval tv{4,0}; int r=select(0,nullptr,&w,&x,&tv);
    if(r==SOCKET_ERROR){ a.wsa=WSAGetLastError(); a.outcome=L"other_error"; ApiErr(L"select",a.wsa); }
    else if(r==0) a.outcome=L"timeout";
    else { int so=0,l=sizeof so; if(getsockopt(s,SOL_SOCKET,SO_ERROR,(char*)&so,&l)!=0){ a.wsa=WSAGetLastError(); a.outcome=L"other_error"; ApiErr(L"getsockopt",a.wsa); }
      else if(so==0){ sockaddr_in pn{}; int pl=sizeof pn; if(getpeername(s,(sockaddr*)&pn,&pl)==0) finish(L"connected",0); else { a.wsa=WSAGetLastError(); a.so=0; a.outcome=L"other_error"; } }
      else { a.wsa=so; finish(so==WSAEACCES?L"denied":(so==WSAECONNREFUSED?L"refused":L"other_error"),so); } } }
  if(a.outcome==L"connected"){ const std::string n8=NonceAscii(id); if(n8.empty()) ApiErr(L"nonce-missing-or-not-hex",0);
    std::string req="GET /p?n="+n8+" HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"; if(send(s,req.c_str(),(int)req.size(),0)==SOCKET_ERROR) ApiErr(L"send",WSAGetLastError()); }
  closesocket(s); g_att.push_back(a); }

std::wstring ReadAllowlisted(const std::wstring& rel, std::string& bytes, std::wstring& mime){
  // restricted handler policy: no "..", no ':', no backslash, only these extensions, file must stay under content root
  if(rel.find(L"..")!=std::wstring::npos||rel.find(L':')!=std::wstring::npos||rel.find(L'\\')!=std::wstring::npos) return L"deny-path";
  static const struct{const wchar_t*e;const wchar_t*m;} ok[]={{L".html",L"text/html"},{L".js",L"text/javascript"},{L".css",L"text/css"},{L".svg",L"image/svg+xml"},{L".png",L"image/png"},{L".woff2",L"font/woff2"},{L".json",L"application/json"}};
  std::wstring ext=PathFindExtensionW(rel.c_str()); std::transform(ext.begin(),ext.end(),ext.begin(),::towlower);
  const wchar_t* m=nullptr; for(auto&o:ok) if(ext==o.e) m=o.m; if(!m) return L"deny-ext"; mime=m;
  std::wstring full=g.content+L"\\"+rel; std::replace(full.begin(),full.end(),L'/',L'\\');
  wchar_t canon[MAX_PATH*2]; if(!PathCanonicalizeW(canon,full.c_str())) return L"deny-canon";
  if(_wcsnicmp(canon,g.content.c_str(),g.content.size())!=0) return L"deny-root";
  std::ifstream f(canon,std::ios::binary); if(!f) return L"notfound"; std::ostringstream ss; ss<<f.rdbuf(); bytes=ss.str(); return L""; }

void Respond(ICoreWebView2WebResourceRequestedEventArgs* a,int code,const wchar_t* reason,const std::string& body,const std::wstring& hdr){
  ComPtr<IStream> st; if(!body.empty()) st.Attach(SHCreateMemStream((const BYTE*)body.data(),(UINT)body.size()));
  ComPtr<ICoreWebView2WebResourceResponse> r; g_env->CreateWebResourceResponse(st.Get(),code,reason,hdr.c_str(),&r); a->put_Response(r.Get()); }

std::wstring CspHeader(){
#ifdef WV2_PROBE
  // PROBE: CSP deliberately open so that only the kernel/network boundary can stop the egress attempts
  return L"Content-Security-Policy: default-src 'self' 'unsafe-inline' http: https: ws: data:\r\nCache-Control: no-store";
#else
  return L"Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'\r\nX-Content-Type-Options: nosniff\r\nCache-Control: no-store";
#endif
}

// Renderer title format (generated fixture): "R|id=outcome;id=outcome". Unparseable or missing ids stay not_attempted/pass-through and are rejected or INCOMPLETE in evaluate.py.
void MergeRendererTitle(){
  if(g_title.rfind(L"R|",0)!=0) return; std::wstringstream ss(g_title.substr(2)); std::wstring kv;
  while(std::getline(ss,kv,L';')){ auto e=kv.find(L'='); if(e==std::wstring::npos) continue; Attempt a; a.id=kv.substr(0,e); a.outcome=kv.substr(e+1); a.so=-1; g_att.push_back(a); } }
void WriteReport(){
  if(g_reported) return; g_reported=true; MergeRendererTitle();
  for(auto id:kAttemptIds){ if(!Att(id)){ Attempt a; a.id=id; a.outcome=std::wstring(id).rfind(L"host_",0)==0?L"not_attempted":L"not_attempted"; g_att.push_back(a); } }
  std::wstring sid; bool ac=SelfIsAppContainer(&sid);
  std::wstringstream o; o<<L"{\"arm\":\""<<g.arm<<L"\",\"host_is_ac\":"<<(ac?L"true":L"false")<<L",\"host_ac_sid\":\""<<sid<<L"\",\"env_hr\":"<<(long)g_envHr<<L",\"ctl_hr\":"<<(long)g_ctlHr<<L",\"attempts\":[";
  for(size_t i=0;i<g_att.size();i++){ auto& a=g_att[i]; std::wstring id=a.id; bool host=id.rfind(L"host_",0)==0;
    o<<(i?L",":L"")<<L"{\"id\":\""<<J(id)<<L"\",\"src\":\""<<(host?L"host":L"renderer")<<L"\",\"outcome\":\""<<J(a.outcome)<<L"\",\"nonce\":\""<<Nonce(id)<<L"\"";
    if(host) o<<L",\"wsa\":"<<a.wsa<<L",\"so_error\":"<<a.so; o<<L"}"; }
  o<<L"],\"api_errors\":[";
  for(size_t i=0;i<g_apiErr.size();i++) o<<(i?L",":L"")<<L"{\"api\":\""<<J(g_apiErr[i].first)<<L"\",\"code\":"<<g_apiErr[i].second<<L"}";
  o<<L"],\"log\":\""<<J(g_log)<<L"\"}";
  std::wstring s=o.str(); std::string u8(WideCharToMultiByte(CP_UTF8,0,s.c_str(),(int)s.size(),0,0,0,0),0); WideCharToMultiByte(CP_UTF8,0,s.c_str(),(int)s.size(),u8.data(),(int)u8.size(),0,0);
  std::ofstream f(g.report,std::ios::binary|std::ios::trunc); f.write(u8.data(),u8.size()); f.flush(); if(!f.good()) ApiErr(L"write-report",0); }

void Hardening(){
  ComPtr<ICoreWebView2Settings> s; g_wv->get_Settings(&s);
  HR(s->put_IsWebMessageEnabled(FALSE));            // no postMessage bridge in either direction
  HR(s->put_AreHostObjectsAllowed(FALSE));          // no AddHostObjectToScript surface
  HR(s->put_AreDevToolsEnabled(FALSE));
  HR(s->put_AreDefaultContextMenusEnabled(FALSE));
  HR(s->put_IsStatusBarEnabled(FALSE));
  HR(s->put_IsZoomControlEnabled(FALSE));
  HR(s->put_AreDefaultScriptDialogsEnabled(FALSE));
  HR(s->put_IsBuiltInErrorPageEnabled(FALSE));
  ComPtr<ICoreWebView2Settings8> s8; if(SUCCEEDED(s.As(&s8))){ BOOL v=FALSE; s8->get_IsReputationCheckingRequired(&v); // DEFAULT left untouched; only read back
    Log(L"smartscreen(IsReputationCheckingRequired)="+std::to_wstring(v)); }
  // NOT used, by design: AddHostObjectToScript, PostWebMessage*, ExecuteScript, AddScriptToExecuteOnDocumentCreated, CallDevToolsProtocolMethod.
}


HRESULT OnNavStarting(ICoreWebView2*,ICoreWebView2NavigationStartingEventArgs* a){
  LPWSTR u; a->get_Uri(&u); std::wstring uri=u; CoTaskMemFree(u);
  bool ok = uri.rfind(std::wstring(L"https://")+kVHost+L"/",0)==0;
#ifdef WV2_PROBE
  ok = true; // probe navigates to loopback/remote targets on purpose to exercise the network service
#endif
  if(!ok){ a->put_Cancel(TRUE); Log(L"nav-blocked"); } return S_OK; }

HRESULT OnWebResourceRequested(ICoreWebView2*,ICoreWebView2WebResourceRequestedEventArgs* a){
  ComPtr<ICoreWebView2WebResourceRequest> rq; a->get_Request(&rq); LPWSTR u,m; rq->get_Uri(&u); rq->get_Method(&m); std::wstring uri=u, meth=m; CoTaskMemFree(u); CoTaskMemFree(m);
  std::wstring pre=std::wstring(L"https://")+kVHost+L"/";
  if(uri.rfind(pre,0)!=0){
#ifdef WV2_PROBE
    return S_OK;
#endif
    Respond(a,403,L"Forbidden","", L"Cache-Control: no-store"); Log(L"req-denied-nonvhost"); return S_OK; }
  if(meth!=L"GET"){ Respond(a,405,L"Method Not Allowed","",L""); return S_OK; }
  std::wstring rel=uri.substr(pre.size()); auto q=rel.find_first_of(L"?#"); if(q!=std::wstring::npos) rel.resize(q); if(rel.empty()) rel=L"index.html";
  std::string body; std::wstring mime; auto err=ReadAllowlisted(rel,body,mime);
  if(!err.empty()){ Respond(a,err==L"notfound"?404:403,L"Denied","",L""); Log(L"serve-"+err); return S_OK; }
  Respond(a,200,L"OK",body,L"Content-Type: "+mime+L"\r\n"+CspHeader()); return S_OK; }

HRESULT OnNavCompleted(ICoreWebView2*,ICoreWebView2NavigationCompletedEventArgs* a){
  BOOL ok; a->get_IsSuccess(&ok); Log(std::wstring(L"nav-completed ok=")+(ok?L"1":L"0"));
#ifdef WV2_PROBE
  SetTimer(g_hwnd,1,25000,nullptr); // let lazily spawned children (gpu, network, utility, crashpad) appear, page finish attempts
#endif
  return S_OK; }

void Wire(){
  HR(g_wv->add_NavigationStarting(Callback<ICoreWebView2NavigationStartingEventHandler>(OnNavStarting).Get(),nullptr));
  HR(g_wv->add_NewWindowRequested(Callback<ICoreWebView2NewWindowRequestedEventHandler>([](ICoreWebView2*,ICoreWebView2NewWindowRequestedEventArgs* a){ a->put_Handled(TRUE); return S_OK; }).Get(),nullptr));
  ComPtr<ICoreWebView2_4> w4; if(SUCCEEDED(g_wv.As(&w4))) HR(w4->add_DownloadStarting(Callback<ICoreWebView2DownloadStartingEventHandler>([](ICoreWebView2*,ICoreWebView2DownloadStartingEventArgs* a){ a->put_Cancel(TRUE); return S_OK; }).Get(),nullptr));
  HR(g_wv->add_PermissionRequested(Callback<ICoreWebView2PermissionRequestedEventHandler>([](ICoreWebView2*,ICoreWebView2PermissionRequestedEventArgs* a){ a->put_State(COREWEBVIEW2_PERMISSION_STATE_DENY); return S_OK; }).Get(),nullptr));
  HR(g_wv->add_ProcessFailed(Callback<ICoreWebView2ProcessFailedEventHandler>([](ICoreWebView2*,ICoreWebView2ProcessFailedEventArgs* a){ COREWEBVIEW2_PROCESS_FAILED_KIND k; a->get_ProcessFailedKind(&k); Log(L"process-failed kind="+std::to_wstring((int)k)); return S_OK; }).Get(),nullptr));
  HR(g_wv->AddWebResourceRequestedFilter(L"*",COREWEBVIEW2_WEB_RESOURCE_CONTEXT_ALL));
  HR(g_wv->add_WebResourceRequested(Callback<ICoreWebView2WebResourceRequestedEventHandler>(OnWebResourceRequested).Get(),nullptr));
#ifdef WV2_PROBE
  HR(g_wv->add_DocumentTitleChanged(Callback<ICoreWebView2DocumentTitleChangedEventHandler>([](ICoreWebView2* s,IUnknown*){ LPWSTR t; s->get_DocumentTitle(&t); g_title=t; CoTaskMemFree(t); if(g_title.rfind(L"R|",0)==0){ WriteReport(); SetTimer(g_hwnd,2,60000,nullptr); } return S_OK; }).Get(),nullptr)); // read-only channel, not a bridge
#endif
  HR(g_wv->add_NavigationCompleted(Callback<ICoreWebView2NavigationCompletedEventHandler>(OnNavCompleted).Get(),nullptr));
}


HRESULT OnControllerReady(HRESULT r2,ICoreWebView2Controller* c){
  g_ctlHr=r2; if(FAILED(r2)){ Log(L"controller-failed"); WriteReport(); PostQuitMessage(31); return S_OK; }
  g_ctl=c; HR(c->get_CoreWebView2(&g_wv)); if(!g_wv){ ApiErr(L"CoreWebView2-null",0); WriteReport(); PostQuitMessage(33); return S_OK; } RECT rc; GetClientRect(g_hwnd,&rc); HR(c->put_Bounds(rc)); Hardening(); Wire();
  std::wstring url=std::wstring(L"https://")+kVHost+L"/index.html";
#ifdef WV2_PROBE
  url+=L"?loop="+g.loop+L"&remote="+g.remote; for(auto&n:g.nonces) if(n.first.rfind(L"rend_",0)==0) url+=L"&n_"+n.first+L"="+n.second;
  TryConnect(L"host_tcp_loopback",g.loop); TryConnect(L"host_tcp_remote",g.remote);
#endif
  HR(g_wv->Navigate(url.c_str())); return S_OK; }

void Start(){
  auto opts=Microsoft::WRL::Make<CoreWebView2EnvironmentOptions>();
  HR(opts->put_Language(L"en-US")); // AdditionalBrowserArguments left EMPTY on purpose (no flags in production; no --no-sandbox ever)
  ComPtr<ICoreWebView2EnvironmentOptions2> o2; if(SUCCEEDED(opts.As(&o2))) HR(o2->put_ExclusiveUserDataFolderAccess(TRUE)); else ApiErr(L"EnvironmentOptions2 unavailable",0);
  HRESULT hr=CreateCoreWebView2EnvironmentWithOptions(g.runtime.c_str(),g.udf.c_str(),opts.Get(),
    Callback<ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler>([](HRESULT r,ICoreWebView2Environment* e){
      g_envHr=r; if(FAILED(r)){ Log(L"env-failed"); WriteReport(); PostQuitMessage(30); return S_OK; } g_env=e;
      HR(e->CreateCoreWebView2Controller(g_hwnd,Callback<ICoreWebView2CreateCoreWebView2ControllerCompletedHandler>(OnControllerReady).Get())); return S_OK; }).Get());
  if(FAILED(hr)){ g_envHr=hr; Log(L"create-env-call-failed"); WriteReport(); PostQuitMessage(32); }
}
LRESULT CALLBACK Proc(HWND h,UINT m,WPARAM w,LPARAM l){
  if(m==WM_TIMER&&w==1){ KillTimer(h,1); WriteReport(); SetTimer(h,2,60000,nullptr); return 0; } // report once, then idle so the launcher can inspect live children
  if(m==WM_TIMER&&w==2){ PostQuitMessage(0); return 0; }
  if(m==WM_SIZE&&g_ctl){ RECT rc; GetClientRect(h,&rc); g_ctl->put_Bounds(rc); }
  if(m==WM_DESTROY){ PostQuitMessage(0); return 0; }
  return DefWindowProcW(h,m,w,l); }
std::wstring Arg(int c,wchar_t** v,const wchar_t* k){ for(int i=1;i+1<c;i++) if(!wcscmp(v[i],k)) return v[i+1]; return L""; }
bool Flag(int c,wchar_t** v,const wchar_t* k){ for(int i=1;i<c;i++) if(!wcscmp(v[i],k)) return true; return false; }
}

int WINAPI wWinMain(HINSTANCE hi,HINSTANCE,PWSTR,int){
  int c; wchar_t** v=CommandLineToArgvW(GetCommandLineW(),&c);
  g.runtime=Arg(c,v,L"--runtime"); g.udf=Arg(c,v,L"--udf"); g.content=Arg(c,v,L"--content"); g.report=Arg(c,v,L"--report");
  g.arm=Arg(c,v,L"--arm"); if(g.arm.empty()) g.arm=L"A"; g.remote=Arg(c,v,L"--remote"); g.loop=Arg(c,v,L"--loop");
  for(int i=1;i+1<c;i++) if(!wcscmp(v[i],L"--nonce")){ std::wstring kv=v[i+1]; auto e=kv.find(L'='); if(e!=std::wstring::npos) g.nonces.push_back({kv.substr(0,e),kv.substr(e+1)}); }
  WSADATA wd; WSAStartup(MAKEWORD(2,2),&wd);
  std::wstring sid; bool ac=SelfIsAppContainer(&sid);
  if(g.arm==L"A"||g.arm==L"B"){ if(!ac){ return 20; } }      // FAIL CLOSED: a contained arm that is not an AppContainer never starts WebView2
  CoInitializeEx(nullptr,COINIT_APARTMENTTHREADED);
  WNDCLASSW wc{}; wc.lpfnWndProc=Proc; wc.hInstance=hi; wc.lpszClassName=L"HotelHRWV2Box"; RegisterClassW(&wc);
  g_hwnd=CreateWindowW(wc.lpszClassName,L"Hotel HR",WS_OVERLAPPEDWINDOW|WS_VISIBLE,CW_USEDEFAULT,CW_USEDEFAULT,1280,800,nullptr,nullptr,hi,nullptr);
  Start(); MSG m; int rc=0; while(GetMessageW(&m,nullptr,0,0)){ TranslateMessage(&m); DispatchMessageW(&m); rc=(int)m.wParam; }
  return rc; }
