"""Deterministic byte patches of the pinned python314.dll for the disc92 diagnostic. Pure functions, no I/O.
Every variant zeroes the PE checksum field and keeps the resource directory and every length intact.
v0: checksum only (control). a: the Common-Controls <dependency> element of the manifest replaced by spaces.
b: control, one comment character of the manifest changed (same length). c: RT_VERSION data zeroed in place."""
import hashlib
import struct

ORIGINAL_SHA256='d8a06cc6ca3348925ea29e343e1c5c5895fe0bb433ff913e1cee44caf358aa6a'
PE_FACTS={'checksum':0x68dce1,'resource':(0x6c3000,0xa30),'entry':0x1e8934,
          'manifest':(0x670aa0,1499),'version':(0x671080,940)}
MANIFEST_SHA256='a6bfbf8d5bca6033c9eac8aa511bf4d7d6655f6c9a6046e63c3dc57b10ff3902'
VERSION_SHA256='0732a3d6397a211103108c8222359e2018f229b7ee2c5413f5b9d1fb319a1dba'
DEP_OPEN=b'<dependency>'
DEP_CLOSE=b'</dependency>'
COMMENT_FROM=b'<!-- Windows Vista -->'
COMMENT_TO=b'<!-- Windows Vistb -->'
VARIANTS={'v0':(),'a':('dep',),'b':('comment',),'c':('version',)}
NAMES={k:'pydisc92_%s.dll'%k for k in VARIANTS}


def _opt(data):
    if data[:2]!=b'MZ':raise ValueError('MZ required')
    pe=struct.unpack_from('<I',data,0x3c)[0]
    if data[pe:pe+4]!=b'PE\0\0':raise ValueError('PE required')
    opt=pe+24
    if struct.unpack_from('<H',data,opt)[0]!=0x20b:raise ValueError('PE32+ required')
    return opt


def dep_span(manifest):
    if manifest.count(DEP_OPEN)!=1 or manifest.count(DEP_CLOSE)!=1:raise ValueError('One dependency element required')
    start=manifest.index(DEP_OPEN);end=manifest.index(DEP_CLOSE)+len(DEP_CLOSE)
    block=manifest[start:end]
    if b'Microsoft.Windows.Common-Controls' not in block or start>end:raise ValueError('Common-Controls dependency required')
    return start,end


def verify_facts(data):
    if hashlib.sha256(data).hexdigest()!=ORIGINAL_SHA256:raise ValueError('Pinned python314.dll required')
    opt=_opt(data)
    f=PE_FACTS
    if struct.unpack_from('<I',data,opt+64)[0]!=f['checksum']:raise ValueError('Checksum fact differs')
    if struct.unpack_from('<II',data,opt+112+2*8)!=f['resource']:raise ValueError('Resource directory fact differs')
    if struct.unpack_from('<I',data,opt+16)[0]!=f['entry']:raise ValueError('Entry point fact differs')
    mo,ml=f['manifest'];vo,vl=f['version']
    if hashlib.sha256(data[mo:mo+ml]).hexdigest()!=MANIFEST_SHA256:raise ValueError('Manifest bytes fact differs')
    if hashlib.sha256(data[vo:vo+vl]).hexdigest()!=VERSION_SHA256:raise ValueError('Version bytes fact differs')
    dep_span(data[mo:mo+ml])
    if data[mo:mo+ml].count(COMMENT_FROM)!=1:raise ValueError('Comment fact differs')
    return opt


def make(data,name):
    if name not in VARIANTS:raise ValueError('Unknown variant')
    opt=verify_facts(data)
    out=bytearray(data)
    struct.pack_into('<I',out,opt+64,0)
    mo,ml=PE_FACTS['manifest'];vo,vl=PE_FACTS['version']
    for part in VARIANTS[name]:
        if part=='dep':
            s,e=dep_span(bytes(out[mo:mo+ml]))
            out[mo+s:mo+e]=b' '*(e-s)
        elif part=='comment':
            s=bytes(out[mo:mo+ml]).index(COMMENT_FROM)
            if len(COMMENT_TO)!=len(COMMENT_FROM):raise ValueError('Comment edit must keep length')
            out[mo+s:mo+s+len(COMMENT_FROM)]=COMMENT_TO
        elif part=='version':
            out[vo:vo+vl]=bytes(vl)
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
