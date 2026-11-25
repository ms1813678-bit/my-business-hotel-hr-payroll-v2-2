/* Review-only ETW controller probe, no start/enable capability.
   Exact session identity by handle; refuses replacement. Windows x64 SDK. */
#define UNICODE
#define _UNICODE
#include <windows.h>
#include <evntrace.h>
#include <stdio.h>
#include <stdlib.h>
#include <stddef.h>
#include <wchar.h>
typedef struct {EVENT_TRACE_PROPERTIES p;WCHAR name[1024];WCHAR file[1024];} DATA;
static void init(DATA *d){ZeroMemory(d,sizeof(*d));d->p.Wnode.BufferSize=sizeof(*d);d->p.LoggerNameOffset=offsetof(DATA,name);d->p.LogFileNameOffset=offsetof(DATA,file);}
int wmain(int argc,wchar_t **argv){DATA d;ULONG s;ULONGLONG expected;wchar_t *end=NULL;
 if(argc<3 || (wcscmp(argv[2],L"HOnYParserFile") && wcscmp(argv[2],L"HOnYParserProcess")))return 2;
 init(&d);s=ControlTraceW(0,argv[2],&d.p,EVENT_TRACE_CONTROL_QUERY);
 if(s==ERROR_WMI_INSTANCE_NOT_FOUND){puts("{\"absent\":true}");return 0;}
 if(s!=ERROR_SUCCESS){printf("{\"query_error\":%lu}\n",s);return 3;}
 if(!wcscmp(argv[1],L"query") && argc==3){printf("{\"absent\":false,\"handle\":\"%I64x\",\"events_lost\":%lu,\"buffers_lost\":%lu,\"realtime_lost\":%lu}\n",d.p.Wnode.HistoricalContext,d.p.EventsLost,d.p.LogBuffersLost,d.p.RealTimeBuffersLost);return 0;}
 if(!wcscmp(argv[1],L"stop-path") && argc==4){
 if(!d.p.LogFileNameOffset || d.p.LogFileNameOffset>=sizeof(d) || _wcsicmp((WCHAR*)((BYTE*)&d+d.p.LogFileNameOffset),argv[3]))return 7;
 expected=d.p.Wnode.HistoricalContext;
 }else{
 if(wcscmp(argv[1],L"stop")||argc!=4)return 2;
 expected=_wcstoui64(argv[3],&end,16);if(!expected||!end||*end||expected!=d.p.Wnode.HistoricalContext)return 4;
 }
 init(&d);s=ControlTraceW(expected,argv[2],&d.p,EVENT_TRACE_CONTROL_STOP);
 if(s!=ERROR_SUCCESS){printf("{\"stop_error\":%lu}\n",s);return 5;}
 printf("{\"stopped\":true,\"events_lost\":%lu,\"buffers_lost\":%lu,\"realtime_lost\":%lu}\n",d.p.EventsLost,d.p.LogBuffersLost,d.p.RealTimeBuffersLost);
 init(&d);s=ControlTraceW(0,argv[2],&d.p,EVENT_TRACE_CONTROL_QUERY);
 return s==ERROR_WMI_INSTANCE_NOT_FOUND?0:6;
}
