/* Read-only installer ownership primitive, not a rollback executor.
   Holds every ancestor with no write/delete sharing; refuses reparse points,
   UNC/device/relative paths and dot/ADS components. Reports volume/file IDs.
   No service, ACL, registry, deletion or write API path. */
#define UNICODE
#define _UNICODE
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <wchar.h>
#include <stdlib.h>
static int valid(const wchar_t *p){
 size_t n=wcslen(p),start=3;
 if(n<4||n>240||!((p[0]>=L'A'&&p[0]<=L'Z')||(p[0]>=L'a'&&p[0]<=L'z'))||p[1]!=L':'||p[2]!=L'\\')return 0;
 for(size_t i=3;i<=n;i++){
  wchar_t c=p[i];
  if(c==L'\\'||!c){
   size_t len=i-start;
   if(!len||(len==1&&p[start]==L'.')||(len==2&&p[start]==L'.'&&p[start+1]==L'.')||p[i-1]==L'.'||p[i-1]==L' ')return 0;
   start=i+1;
  }else if(c<32||c==L'/'||c==L':'||c==L'"'||c==L'<'||c==L'>'||c==L'|'||c==L'?'||c==L'*')return 0;
 }
 return 1;
}
int wmain(int argc,wchar_t **argv){
 HANDLE held[128];DWORD count=0;wchar_t path[256],final[512];size_t n;int result=1;
 if((argc!=2&&!(argc==3&&!wcscmp(argv[2],L"--hold-fixture-10s")))||!valid(argv[1])){puts("{\"refused\":\"path_shape\"}");return 2;}
 n=wcslen(argv[1]);wcscpy_s(path,256,argv[1]);
 for(size_t end=3;end<=n;end++){
  BY_HANDLE_FILE_INFORMATION info;DWORD got;wchar_t saved;
  if(end!=3&&end!=n&&path[end]!=L'\\')continue;
  if(count>=128)goto done;
  saved=path[end];path[end]=0;
  held[count]=CreateFileW(path,FILE_READ_ATTRIBUTES,FILE_SHARE_READ,NULL,OPEN_EXISTING,FILE_FLAG_BACKUP_SEMANTICS|FILE_FLAG_OPEN_REPARSE_POINT,NULL);
  path[end]=saved;
  if(held[count]==INVALID_HANDLE_VALUE){printf("{\"refused\":\"open\",\"error\":%lu}\n",GetLastError());goto done;}
  count++;
  if(!GetFileInformationByHandle(held[count-1],&info))goto done;
  if((info.dwFileAttributes&FILE_ATTRIBUTE_REPARSE_POINT)||((end<n)&&!(info.dwFileAttributes&FILE_ATTRIBUTE_DIRECTORY))){puts("{\"refused\":\"reparse_or_parent_type\"}");goto done;}
  got=GetFinalPathNameByHandleW(held[count-1],final,512,FILE_NAME_NORMALIZED|VOLUME_NAME_DOS);
  if(!got||got>=512||wcsncmp(final,L"\\\\?\\",4))goto done;
  /* Require ordinary spelling, no short-name/device/path alias fallback. */
  saved=path[end];path[end]=0;
  if(_wcsicmp(final+4,path)){path[end]=saved;puts("{\"refused\":\"resolved_path_mismatch\"}");goto done;}
  path[end]=saved;
  if(end==n){
   if(!(info.dwFileAttributes&FILE_ATTRIBUTE_DIRECTORY)&&info.nNumberOfLinks!=1){puts("{\"refused\":\"multiple_hardlinks\"}");goto done;}
   printf("{\"held\":true,\"ancestors\":%lu,\"volume\":%lu,\"file_id_high\":%lu,\"file_id_low\":%lu,\"links\":%lu,\"directory\":%s}\n",count,info.dwVolumeSerialNumber,info.nFileIndexHigh,info.nFileIndexLow,info.nNumberOfLinks,(info.dwFileAttributes&FILE_ATTRIBUTE_DIRECTORY)?"true":"false");
   fflush(stdout);if(argc==3)Sleep(10000);
   result=0;
  }
 }
 done:
 while(count){count--;if(!CloseHandle(held[count]))result=3;}
 return result;
}
