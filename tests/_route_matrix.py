"""Static route/guard matrix. AST only; imports nothing from the app."""
import ast, glob, json, os, sys
HTTP = {"get", "post", "put", "delete", "patch", "api_route"}

def dname(n):
    if isinstance(n, ast.Name): return n.id
    if isinstance(n, ast.Attribute): return dname(n.value) + "." + n.attr
    if isinstance(n, ast.Call): return dname(n.func) + "()"
    return "?"

def depends_in(node):
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and dname(n.func).split(".")[-1] == "Depends" and n.args:
            out.append(ast.unparse(n.args[0]))
    return out

def fold(n, env):
    if isinstance(n, ast.Constant) and isinstance(n.value, str): return n.value
    if isinstance(n, ast.Name): return env.get(n.id)
    if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
        a, b = fold(n.left, env), fold(n.right, env)
        return a + b if a is not None and b is not None else None
    if isinstance(n, ast.JoinedStr):
        out = ""
        for v in n.values:
            if isinstance(v, ast.Constant): out += v.value
            else:
                f = fold(v.value, env) if isinstance(v, ast.FormattedValue) else None
                out += f if f is not None else "{" + ast.unparse(v.value) + "}"
        return out
    return None

def scan(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    env = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            v = fold(n.value, env)
            if v is not None: env[n.targets[0].id] = v
    rows = []
    # router-level dependencies
    router_deps = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and dname(n.value.func).endswith("APIRouter"):
            for t in n.targets:
                router_deps[dname(t)] = [d for kw in n.value.keywords if kw.arg == "dependencies" for d in depends_in(kw.value)]
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)): continue
        for d in n.decorator_list:
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in HTTP and d.args:
                owner = dname(d.func.value)
                deps = list(router_deps.get(owner, []))
                deps += [x for kw in d.keywords if kw.arg == "dependencies" for x in depends_in(kw.value)]
                deps += depends_in(n.args)
                rows.append(dict(file=os.path.basename(path), method=d.func.attr.upper(), path=fold(d.args[0], env) or ("UNRESOLVED:" + ast.unparse(d.args[0])),
                                 func=n.name, deps=sorted(set(deps))))
    return rows

def all_rows(root):
    rows = []
    for p in sorted(glob.glob(os.path.join(root, "*.py"))):
        if os.path.basename(p) == "dependencies.py": continue
        rows += scan(p)
    return rows

if __name__ == "__main__":
    rows = all_rows(sys.argv[1])
    print(json.dumps(rows, ensure_ascii=False, indent=1))
