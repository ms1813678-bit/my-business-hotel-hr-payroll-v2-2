/* Local preparatory parent controls. All sockets literal loopback, no DNS/external.
 * No native execution yet. Each listener remains parent-owned until child ends. */
#define _WIN32_WINNT 0x0A00
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <stdio.h>
#include <string.h>
struct listener_control {SOCKET listener;int family,datagram;unsigned short port;int positive_verified;};
static int wait_socket(SOCKET s,int writing) {
 fd_set f,x;FD_ZERO(&f);FD_ZERO(&x);FD_SET(s,&f);FD_SET(s,&x);struct timeval tv={1,0};
 return select(0,writing?NULL:&f,writing?&f:NULL,&x,&tv)>0 && FD_ISSET(s,&f);
}
static int timeout_socket(SOCKET s) {
 DWORD ms=1000;
 return setsockopt(s,SOL_SOCKET,SO_RCVTIMEO,(void*)&ms,sizeof(ms))==0 && setsockopt(s,SOL_SOCKET,SO_SNDTIMEO,(void*)&ms,sizeof(ms))==0;
}
static int addr(int family,unsigned short port,struct sockaddr_storage *p) {
 memset(p,0,sizeof(*p));
 if(family==AF_INET){struct sockaddr_in *a=(void*)p;a->sin_family=AF_INET;a->sin_port=htons(port);a->sin_addr.s_addr=htonl(INADDR_LOOPBACK);return sizeof(*a);}
 struct sockaddr_in6 *a=(void*)p;a->sin6_family=AF_INET6;a->sin6_port=htons(port);a->sin6_addr=in6addr_loopback;return sizeof(*a);
}
static int exact_send(SOCKET s,const char *buf,int n){
 int sent=0;while(sent<n){int r=send(s,buf+sent,n-sent,0);if(r<=0)return 0;sent+=r;}return 1;
}
static int exact_receive(SOCKET s,char *buf,int n){
 int got=0;while(got<n){int r=recv(s,buf+got,n-got,0);if(r<=0)return 0;got+=r;}return 1;
}
int verify_positive_listener(struct listener_control *c,const char *nonce){
 SOCKET client=INVALID_SOCKET,accepted=INVALID_SOCKET;struct sockaddr_storage a,source;int family=c->family,datagram=c->datagram,len,source_len=sizeof(source),ok=0;char received[128]={0};size_t n=strlen(nonce);
 if(n<16 || n>64 || c->listener==INVALID_SOCKET)return 0;
 len=sizeof(a);if(getsockname(c->listener,(void*)&a,&len))return 0;
 unsigned short actual_port=family==AF_INET?ntohs(((struct sockaddr_in*)&a)->sin_port):ntohs(((struct sockaddr_in6*)&a)->sin6_port);
 if(actual_port!=c->port)return 0;
 client=socket(family,datagram?SOCK_DGRAM:SOCK_STREAM,datagram?IPPROTO_UDP:IPPROTO_TCP);
 if(client==INVALID_SOCKET || !timeout_socket(client))goto done;
 if(datagram){
  if(sendto(client,nonce,(int)n,0,(void*)&a,len)!=(int)n || !wait_socket(c->listener,0))goto done;
  int got=recvfrom(c->listener,received,sizeof(received),0,(void*)&source,&source_len);
  if(got!=(int)n || memcmp(received,nonce,n))goto done;
  if(sendto(c->listener,nonce,(int)n,0,(void*)&source,source_len)!=(int)n || !wait_socket(client,0))goto done;
  struct sockaddr_storage reply={0};int reply_len=sizeof(reply);
  got=recvfrom(client,received,sizeof(received),0,(void*)&reply,&reply_len);
  if(got!=(int)n || memcmp(received,nonce,n) || reply.ss_family!=family)goto done;
  if(family==AF_INET){struct sockaddr_in *r=(void*)&reply,*expected=(void*)&a;if(r->sin_port!=expected->sin_port || r->sin_addr.s_addr!=expected->sin_addr.s_addr)goto done;}
  else {struct sockaddr_in6 *r=(void*)&reply,*expected=(void*)&a;if(r->sin6_port!=expected->sin6_port || memcmp(&r->sin6_addr,&expected->sin6_addr,sizeof(r->sin6_addr)) || r->sin6_scope_id!=expected->sin6_scope_id)goto done;}
 }else{
  u_long nb=1;if(ioctlsocket(client,FIONBIO,&nb))goto done;
  int rc=connect(client,(void*)&a,len),e=rc?WSAGetLastError():0;
  if(rc && (e!=WSAEWOULDBLOCK || !wait_socket(client,1)))goto done;
  int so=0,sl=sizeof(so);if(getsockopt(client,SOL_SOCKET,SO_ERROR,(void*)&so,&sl)||so)goto done;
  if(!wait_socket(c->listener,0))goto done;
  accepted=accept(c->listener,NULL,NULL);if(accepted==INVALID_SOCKET || !timeout_socket(accepted))goto done;
  nb=0;if(ioctlsocket(client,FIONBIO,&nb))goto done;
  if(!exact_send(client,nonce,(int)n))goto done;
  if(!exact_receive(accepted,received,(int)n) || memcmp(received,nonce,n))goto done;
  if(!exact_send(accepted,nonce,(int)n))goto done;
  if(!exact_receive(client,received,(int)n) || memcmp(received,nonce,n))goto done;
 }
 ok=1;
 done:
 if(client!=INVALID_SOCKET && closesocket(client))ok=0;
 if(accepted!=INVALID_SOCKET && closesocket(accepted))ok=0;
 return ok;
}
int setup_positive_listener(struct listener_control *c,int family,int datagram,const char *nonce) {
 struct sockaddr_storage a;int len,ok=0;size_t n=strlen(nonce);
 c->listener=INVALID_SOCKET;c->family=family;c->datagram=datagram;c->port=0;c->positive_verified=0;
 if(n<16 || n>64)return 0;
 c->listener=socket(family,datagram?SOCK_DGRAM:SOCK_STREAM,datagram?IPPROTO_UDP:IPPROTO_TCP);
 if(c->listener==INVALID_SOCKET)goto done;
 BOOL exclusive=TRUE;
 if(setsockopt(c->listener,SOL_SOCKET,SO_EXCLUSIVEADDRUSE,(void*)&exclusive,sizeof(exclusive)))goto done;
 if(!timeout_socket(c->listener))goto done;
 len=addr(family,0,&a);if(bind(c->listener,(void*)&a,len))goto done;
 if(getsockname(c->listener,(void*)&a,&len))goto done;
 c->port=family==AF_INET?ntohs(((struct sockaddr_in*)&a)->sin_port):ntohs(((struct sockaddr_in6*)&a)->sin6_port);
 if(!datagram && listen(c->listener,4))goto done;
 ok=verify_positive_listener(c,nonce);
 done:
 if(!ok && c->listener!=INVALID_SOCKET){closesocket(c->listener);c->listener=INVALID_SOCKET;}
 c->positive_verified=ok;return ok;
}
