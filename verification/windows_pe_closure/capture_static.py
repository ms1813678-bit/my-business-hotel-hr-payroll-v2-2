"""Read-only static closure diagnostic, no analyzed-binary execution or loader probes."""
import base64,hashlib,json,os,pathlib,shutil,struct,sys,urllib.request,urllib.parse,zipfile
from apisetschema.namespace import parse_namespace
from apisetschema.mapping import build_mapping,full_report
from peinspect.pe import parse_pe,read_capped
from peinspect.closure import run_closure,is_apiset_name,forwarder_target
HERE=pathlib.Path(__file__).resolve().parent

def main():
    if sys.platform!='win32' or os.environ.get('GITHUB_ACTIONS')!='true':raise RuntimeError('disposable Windows CI only')
    root=pathlib.Path(os.environ['RUNNER_TEMP'])/'hony-pe-closure-static'
    if root.exists():raise RuntimeError('fresh static fixture root required')
    root.mkdir();runtime=root/'runtime';runtime.mkdir();snapshot=root/'system-snapshot';snapshot.mkdir()
    output=pathlib.Path(os.environ['RUNNER_TEMP'])/'hony-pe-closure-evidence';output.mkdir(exist_ok=False)
    lock=json.loads((HERE/'ACQUISITION-LOCK.json').read_text())['python']
    class SameSupplier(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,req,fp,code,msg,headers,newurl):
            if urllib.parse.urlparse(newurl).scheme!='https' or urllib.parse.urlparse(newurl).hostname!='www.python.org':raise RuntimeError('supplier redirect refused')
            return super().redirect_request(req,fp,code,msg,headers,newurl)
    try:
        with urllib.request.build_opener(SameSupplier).open(lock['url'],timeout=45) as response:data=response.read(30_000_001)
        if len(data)>30_000_000 or hashlib.sha256(data).hexdigest()!=lock['sha256']:raise RuntimeError('locked runtime archive hash refused')
        import io
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if len(z.infolist())>300 or sum(x.file_size for x in z.infolist())>150_000_000:raise RuntimeError('archive bounds')
            for info in z.infolist():
                p=pathlib.PurePosixPath(info.filename)
                if p.is_absolute() or '..' in p.parts or len(p.parts)!=1 or ':' in info.filename or '\\' in info.filename:raise RuntimeError('archive path refused')
                (runtime/p.name).write_bytes(z.read(info))
        sentinel=root/'binary-sentinel'
        sentinel_bytes=bytes(range(256))*500
        sentinel.write_bytes(sentinel_bytes)
        if read_capped(str(sentinel))!=sentinel_bytes:raise RuntimeError('native binary all-byte read regression')
        print('NATIVE_BINARY_ALL_BYTES_CRLF_CTRLZ_PASS',flush=True)
        expected=json.loads((HERE/'EXPECTED-DIRECT-IMPORTS.json').read_text())
        for name,want in expected['members'].items():
            raw=read_capped(str(runtime/name))
            if hashlib.sha256(raw).hexdigest()!=want['sha256']:raise RuntimeError('binary read hash regression: '+name)
            image=parse_pe(raw)
            if hashlib.sha256(raw).hexdigest()!=want['sha256'] or [m.dll_name for m in image.imports]!=want['imports']:raise RuntimeError('actual pinned direct-import regression')
        # Enumerate names once, then copy ONLY requested static candidates from
        # exact System32 into an owned snapshot. Never call LoadLibrary, resolve
        # via PATH, capture registry/token state or claim actual loader choice.
        system=pathlib.Path(os.environ['SystemRoot'])/'System32';index={}
        for count,p in enumerate(system.iterdir(),1):
            if count>30000:raise RuntimeError('system candidate name-count bound')
            if p.is_file() and not p.is_symlink():index.setdefault(p.name.lower(),[]).append(p)
        queue=sorted([p for p in runtime.iterdir() if p.suffix.lower() in ('.exe','.dll','.pyd')]);seen=set();capture=[];parse_errors=[];total=0
        while queue:
            p=queue.pop(0)
            if str(p).lower() in seen:continue
            seen.add(str(p).lower())
            if len(seen)>512:raise RuntimeError('static module-count bound')
            try:image=parse_pe(read_capped(str(p)))
            except Exception as exc:
                parse_errors.append(dict(file=p.name,error_type=type(exc).__name__,reason=str(exc)));continue
            names=[m.dll_name for m in image.imports+image.delay_imports]
            names += [n for e in image.exports if e.forwarder for n in [forwarder_target(e.forwarder)] if n]
            for name in sorted(set(names),key=str.lower):
                if pathlib.PureWindowsPath(name).name!=name or '/' in name or '\\' in name:raise RuntimeError('dependency name not basename')
                if is_apiset_name(name) or any(q.name.lower()==name.lower() for q in runtime.iterdir()) or (snapshot/name.lower()).exists():continue
                candidates=index.get(name.lower(),[])
                if len(candidates)!=1:
                    capture.append(dict(requested=name,status='not-present' if not candidates else 'ambiguous'));continue
                source=candidates[0];raw=read_capped(str(source));total+=len(raw)
                if total>512*1024*1024:raise RuntimeError('snapshot total byte bound')
                target=snapshot/name.lower();target.write_bytes(raw)
                if hashlib.sha256(target.read_bytes()).digest()!=hashlib.sha256(raw).digest():raise RuntimeError('snapshot copy mismatch')
                capture.append(dict(requested=name,status='candidate-snapshot',source=str(source),sha256=hashlib.sha256(raw).hexdigest(),size=len(raw)))
                queue.append(target)
        # Capture exact owned read-only namespace section bytes, no interpretation
        # or mapping use. Standalone evidence awaits source-qualified parser.
        schema_source=system/'apisetschema.dll'
        schema_raw=read_capped(str(schema_source));schema_image=parse_pe(schema_raw)
        sections=[section for section in schema_image.sections if section.name=='.apiset']
        if len(sections)!=1:raise RuntimeError('unique API-set namespace section required')
        section=sections[0]
        if not 4<=section.virtual_size<=4*1024*1024 or section.virtual_size>section.raw_size:raise RuntimeError('bounded file-backed API-set namespace required')
        namespace=schema_raw[section.raw_offset:section.raw_offset+section.virtual_size]
        if len(namespace)!=section.virtual_size:raise RuntimeError('truncated namespace capture')
        (output/'apiset-namespace-capture.json').write_text(json.dumps(dict(status='uninterpreted-static-bytes',source=str(schema_source),file_sha256=hashlib.sha256(schema_raw).hexdigest(),file_bytes=len(schema_raw),section_sha256=hashlib.sha256(namespace).hexdigest(),section_bytes=len(namespace),section_raw_offset=section.raw_offset,observed_first_u32=struct.unpack_from('<I',namespace)[0],section_base64=base64.b64encode(namespace).decode('ascii'),os_build=sys.getwindowsversion().build,notice='No namespace mapping or layout acceptance. Exact raw section bytes for independent review only.'),indent=2)+'\n')
        print('READONLY_UNINTERPRETED_APISET_SECTION_CAPTURE_PASS',flush=True)
        parsed_namespace=parse_namespace(namespace)
        proposed_mapping=build_mapping(parsed_namespace)
        namespace_report=full_report(parsed_namespace,proposed_mapping,source_path=str(schema_source),os_build=str(sys.getwindowsversion().build),image_sha256=hashlib.sha256(schema_raw).hexdigest())
        namespace_report['notice']='Read-only source-derived static namespace evidence. Candidate mapping NOT consumed by closure; SEALED/EXTENSION/context remain blocked. No loader resolution or security claim.'
        (output/'apiset-static-review.json').write_text(json.dumps(namespace_report,indent=2)+'\n')
        for offset,value in [(4,28),(8,2),(8,3)]:
            mutated=bytearray(namespace);struct.pack_into('<I',mutated,offset,value)
            if offset==4:
                try:parse_namespace(bytes(mutated))
                except ValueError:pass
                else:raise RuntimeError('namespace Size bypass regression')
            elif build_mapping(parse_namespace(bytes(mutated))).mapping:raise RuntimeError('namespace extension mapping regression')
        print('STATIC_NAMESPACE_SOURCE_BYTES_HASH_SIZE_EXTENSION_REGRESSIONS_PASS',len(parsed_namespace.entries),len(proposed_mapping.mapping),flush=True)
        report=run_closure([str(runtime/'python.exe')],[dict(label='locked-runtime',path=str(runtime)),dict(label='system32-requested-snapshot',path=str(snapshot))])
        report['capture']=dict(runtime_archive_sha256=lock['sha256'],os_version=sys.getwindowsversion()._asdict() if hasattr(sys.getwindowsversion(),'_asdict') else str(sys.getwindowsversion()),system32_candidates=capture,system32_index='all regular nonsymlink basenames including DLL/DRV, bounded30000; absence means only not indexed here',parse_errors=parse_errors,notice='Static requested-file snapshot only. No API-set mapping captured; no actual loader resolution, restricted access or network claim.')
        (output/'static-closure.json').write_text(json.dumps(report,indent=2)+'\n')
        print('ACTUAL_PINNED_PE_DIRECT_IMPORTS_PASS',flush=True)
        print('STATIC_CLOSURE_SUMMARY',json.dumps(report['summary']),flush=True)
        print('STATIC_ONLY_C0000022_CAUSE_NETWORK_NOT_ESTABLISHED',flush=True)
    except BaseException as exc:
        (output/'static-failure.json').write_text(json.dumps(dict(status='failed',error_type=type(exc).__name__,reason=str(exc),static_only=True),indent=2)+'\n')
        raise
    finally:
        shutil.rmtree(root)
        if root.exists():raise RuntimeError('owned static root cleanup unconfirmed')
        print('OWNED_STATIC_ANALYSIS_ROOT_ABSENT_PASS',flush=True)

if __name__=='__main__':main()
