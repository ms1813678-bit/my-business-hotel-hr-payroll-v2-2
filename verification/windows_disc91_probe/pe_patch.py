"""Deterministic byte patches of the pinned python314.dll for the disc91 diagnostic. Pure functions, no I/O.
Each variant zeroes the PE checksum field. V0 changes nothing else. V1 also zeroes the TLS directory entry,
V2 the resource directory entry, V3 patches the entry point to 'mov eax,1; ret', V4 does V1+V2+V3."""
import hashlib
import struct

ORIGINAL_SHA256='d8a06cc6ca3348925ea29e343e1c5c5895fe0bb433ff913e1cee44caf358aa6a'
PE_FACTS={'checksum':0x68dce1,'tls':(0x525f00,0x28),'resource':(0x6c3000,0xa30),'entry':0x1e8934,'loadcfg':(0x3b2fa0,0x140),
          'text_va':0x1000,'text_raw':0x400,'text_rawsize':0x325c00,'entry_bytes':'48895c24084889742410574883ec2049'}
PATCH=bytes.fromhex('b801000000c3')
VARIANTS={'v0':(),'v1':('tls',),'v2':('resource',),'v3':('entry',),'v4':('tls','resource','entry')}
NAMES={k:'pydisc91_%s.dll'%k for k in VARIANTS}


def _offsets(data):
    if data[:2]!=b'MZ':raise ValueError('MZ required')
    pe=struct.unpack_from('<I',data,0x3c)[0]
    if data[pe:pe+4]!=b'PE\0\0':raise ValueError('PE required')
    opt=pe+24
    if struct.unpack_from('<H',data,opt)[0]!=0x20b:raise ValueError('PE32+ required')
    return opt


def verify_facts(data):
    if hashlib.sha256(data).hexdigest()!=ORIGINAL_SHA256:raise ValueError('Pinned python314.dll required')
    opt=_offsets(data)
    f=PE_FACTS
    if struct.unpack_from('<I',data,opt+64)[0]!=f['checksum']:raise ValueError('Checksum fact differs')
    if struct.unpack_from('<II',data,opt+112+9*8)!=f['tls']:raise ValueError('TLS directory fact differs')
    if struct.unpack_from('<II',data,opt+112+2*8)!=f['resource']:raise ValueError('Resource directory fact differs')
    if struct.unpack_from('<II',data,opt+112+10*8)!=f['loadcfg']:raise ValueError('Load config fact differs')
    if struct.unpack_from('<I',data,opt+16)[0]!=f['entry']:raise ValueError('Entry point fact differs')
    off=f['entry']-f['text_va']+f['text_raw']
    if not (f['text_raw']<=off and off+len(PATCH)<=f['text_raw']+f['text_rawsize']):raise ValueError('Entry outside text raw data')
    if data[off:off+16].hex()!=f['entry_bytes']:raise ValueError('Entry bytes fact differs')
    return opt,off


def make(data,name):
    if name not in VARIANTS:raise ValueError('Unknown variant')
    opt,off=verify_facts(data)
    out=bytearray(data)
    struct.pack_into('<I',out,opt+64,0)
    for part in VARIANTS[name]:
        if part=='tls':struct.pack_into('<II',out,opt+112+9*8,0,0)
        elif part=='resource':struct.pack_into('<II',out,opt+112+2*8,0,0)
        elif part=='entry':out[off:off+len(PATCH)]=PATCH
    if len(out)!=len(data):raise ValueError('Length changed')
    return bytes(out)


def changed_ranges(a,b):
    out=[];i=0
    while i<len(a):
        if a[i]!=b[i]:
            j=i
            while j<len(a) and a[j]!=b[j]:j+=1
            out.append((i,j-i));i=j
        else:i+=1
    return out
