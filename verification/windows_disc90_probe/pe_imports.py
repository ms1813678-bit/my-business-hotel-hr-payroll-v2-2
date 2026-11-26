"""Minimal PE32+ direct-import-table reader (names in table order). Static facts only."""
import struct


def direct_imports(data):
    if type(data) is not bytes or data[:2] != b'MZ' or len(data) < 0x100:raise ValueError('MZ image required')
    pe=struct.unpack_from('<I',data,0x3c)[0]
    if data[pe:pe+4] != b'PE\0\0':raise ValueError('PE signature required')
    nsec,=struct.unpack_from('<H',data,pe+6);osz,=struct.unpack_from('<H',data,pe+20)
    opt=pe+24
    if struct.unpack_from('<H',data,opt)[0] != 0x20b:raise ValueError('PE32+ required')
    rva,_size=struct.unpack_from('<II',data,opt+112+8)
    secs=[]
    for i in range(nsec):
        vsz,va,rsz,ro=struct.unpack_from('<IIII',data,opt+osz+40*i+8)
        secs.append((va,max(vsz,rsz),ro))
    def off(r):
        for va,sz,ro in secs:
            if va <= r < va+sz:return r-va+ro
        raise ValueError('RVA outside sections')
    out=[]
    if not rva:return out
    p=off(rva)
    while True:
        d=struct.unpack_from('<IIIII',data,p)
        if d == (0,0,0,0,0):break
        n=off(d[3]);e=data.index(b'\0',n)
        out.append(data[n:e].decode('ascii'));p+=20
        if len(out) > 200:raise ValueError('Import table bound')
    return out
