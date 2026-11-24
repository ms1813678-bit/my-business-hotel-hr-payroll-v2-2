/* Preparatory fixed synthetic child resources. Not integrated or run on Windows.
 * Parent must validate exact image, root provenance/ACLs, gate token/job/caps and
 * launch only this fixed code with an owned inherited result channel.
 * No production data, arbitrary payload, DNS, external network or deletion. */
#define _WIN32_WINNT 0x0A00
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <stdio.h>
#include <wchar.h>
#include <string.h>
struct resource_paths { const wchar_t *allowed_read,*forbidden_read,*allowed_create,*forbidden_create; };
struct resource_result { const char *test;int api_ok;DWORD error;int accepted; };
static int emit(struct resource_result r) {
 fprintf(stdout,"resource test=%s api_ok=%d error=%lu accepted=%d\n",r.test,r.api_ok,(unsigned long)r.error,r.accepted);
 return r.accepted;
}
static int exact_read(const wchar_t *path) {
 static const char expected[]="HONY-SYNTHETIC-ALLOWED-READ-v1";
 char b[sizeof(expected)]={0};DWORD got=0;struct resource_result r={"allowed_read",0,0,0};
 HANDLE h=CreateFileW(path,GENERIC_READ,FILE_SHARE_READ,NULL,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,NULL);
 if(h==INVALID_HANDLE_VALUE){r.error=GetLastError();return emit(r);}
 SetLastError(0);r.api_ok=ReadFile(h,b,sizeof(b),&got,NULL);r.error=GetLastError();
 r.accepted=r.api_ok && got==sizeof(expected)-1 && !memcmp(b,expected,sizeof(expected)-1);
 if(!CloseHandle(h))r.accepted=0;
 return emit(r);
}
static int denied_open(const wchar_t *path,int create) {
 struct resource_result r={create?"forbidden_create":"forbidden_read",0,0,0};
 SetLastError(0);HANDLE h=CreateFileW(path,create?GENERIC_WRITE:GENERIC_READ,FILE_SHARE_READ,NULL,create?CREATE_NEW:OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,NULL);
 r.error=GetLastError();r.api_ok=h!=INVALID_HANDLE_VALUE;
 r.accepted=!r.api_ok && r.error==ERROR_ACCESS_DENIED;
 if(h!=INVALID_HANDLE_VALUE && !CloseHandle(h))r.accepted=0;
 return emit(r);
}
static int exact_write(const wchar_t *path) {
 static const char marker[]="HONY-SYNTHETIC-ALLOWED-WRITE-v1";DWORD got=0;
 struct resource_result r={"allowed_create",0,0,0};
 HANDLE h=CreateFileW(path,GENERIC_WRITE,0,NULL,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,NULL);
 if(h==INVALID_HANDLE_VALUE){r.error=GetLastError();return emit(r);}
 SetLastError(0);r.api_ok=WriteFile(h,marker,sizeof(marker)-1,&got,NULL);r.error=GetLastError();
 r.accepted=r.api_ok && got==sizeof(marker)-1;
 if(!CloseHandle(h))r.accepted=0;
 return emit(r);
}
/* Tests only immediate kernel rejection for literal loopback. Parent must first
 * prove nonce/ack on exact listening endpoint outside sandbox. API failures other
 * than WSAEACCES never pass; no timeout/refused connection counted as denied. */
static int denied_loopback(int family,int datagram,unsigned short port) {
 struct resource_result r={family==AF_INET?(datagram?"udp4_loopback":"tcp4_loopback"):(datagram?"udp6_loopback":"tcp6_loopback"),0,0,0};
 SOCKET s=socket(family,datagram?SOCK_DGRAM:SOCK_STREAM,datagram?IPPROTO_UDP:IPPROTO_TCP);
 if(s==INVALID_SOCKET){r.error=(DWORD)WSAGetLastError();r.accepted=r.error==WSAEACCES;return emit(r);}
 u_long nonblock=1;
 if(ioctlsocket(s,FIONBIO,&nonblock)==SOCKET_ERROR){r.error=(DWORD)WSAGetLastError();closesocket(s);return emit(r);}
 struct sockaddr_storage store={0};int len;
 if(family==AF_INET){struct sockaddr_in *a=(void*)&store;a->sin_family=AF_INET;a->sin_port=htons(port);a->sin_addr.s_addr=htonl(INADDR_LOOPBACK);len=sizeof(*a);}
 else {struct sockaddr_in6 *a=(void*)&store;a->sin6_family=AF_INET6;a->sin6_port=htons(port);a->sin6_addr=in6addr_loopback;len=sizeof(*a);}
 int rc=datagram?sendto(s,"HONY-FIXED-PROBE",16,0,(void*)&store,len):connect(s,(void*)&store,len);
 r.api_ok=rc!=SOCKET_ERROR;r.error=r.api_ok?0:(DWORD)WSAGetLastError();r.accepted=!r.api_ok && r.error==WSAEACCES;
 if(closesocket(s)==SOCKET_ERROR)r.accepted=0;
 return emit(r);
}
int run_fixed_child_resources(const struct resource_paths *p,const unsigned short ports[4]) {
 int success=1;WSADATA wd;
 success&=exact_read(p->allowed_read);
 success&=denied_open(p->forbidden_read,0);
 success&=exact_write(p->allowed_create);
 success&=denied_open(p->forbidden_create,1);
 if(WSAStartup(MAKEWORD(2,2),&wd))return 1;
 success&=denied_loopback(AF_INET,0,ports[0]);success&=denied_loopback(AF_INET6,0,ports[1]);
 success&=denied_loopback(AF_INET,1,ports[2]);success&=denied_loopback(AF_INET6,1,ports[3]);
 if(WSACleanup()==SOCKET_ERROR)success=0;
 if(fflush(stdout))success=0;
 return success?0:1;
}
