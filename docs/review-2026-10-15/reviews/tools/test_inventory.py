"""AST inventory of every test module: counts, assertion mix, pins and risky dependencies."""
import ast, os, re, sys, json, collections
ROOT = "/home/user/alpaca-trading"
HEX64 = re.compile(r"\b[0-9a-f]{64}\b"); HEX16 = re.compile(r"\b[0-9a-f]{16}\b")
mods = []
for dirpath, _, files in os.walk(os.path.join(ROOT, "tests")):
    for fn in sorted(files):
        if fn.startswith("test_") and fn.endswith(".py"):
            mods.append(os.path.join(dirpath, fn))
tot = collections.Counter(); rows = []
critical = {
    "second exposure same symbol (B-01)": r"(second|two|another).{0,40}(day|session|exposure|trade).{0,80}(close|flatten)|client_order_id.{0,60}(reuse|collid|unique)",
    "flatten books close (C1-01)": r"flatten.{0,120}(journal|outcome|active_trades|close row)",
    "partial fill": r"partial",
    "HTTP 429 / rate limit": r"\b429\b|rate.?limit|too many requests",
    "timeout / hung call (B-10)": r"timeout|hang|hung",
    "early close 13:00": r"early.?close|13:00|half.?day",
    "DST transition": r"\bdst\b|daylight|november 1|nov 1|march 8|2026-03-08|2026-11-01",
    "halt / LULD": r"\bhalt|luld",
    "easy_to_borrow / shortable": r"easy_to_borrow|shortable|hard.to.borrow",
    "gap through stop": r"gap.{0,30}stop|gapped",
    "exit_before deadline semantics": r"exit_before",
    "stop-first tie": r"stop.?first|tie_broken|tie",
    "next-bar-open entry": r"next.?bar|entry_bar|next_open",
    "look-ahead / causal": r"look.?ahead|lookahead|causal|future",
    "notional cap binds (B-06)": r"max_position_notional|notional_cap",
}
crit_hits = collections.defaultdict(list)
for path in mods:
    src = open(path).read(); rel = os.path.relpath(path, ROOT)
    try: tree = ast.parse(src)
    except SyntaxError as e: rows.append({"module": rel, "error": str(e)}); continue
    tests = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")]
    calls = collections.Counter()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr.startswith("assert"):
            calls[n.func.attr] += 1
    strs = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    hex64 = sum(1 for s in strs if HEX64.search(s)); hex16 = sum(1 for s in strs if HEX16.search(s))
    reads_docs = len(re.findall(r"\.md[\"']|README|ARCHITECTURE|OPERATIONS|SETUP\.md|protocol\.md", src))
    wall = len(re.findall(r"time\.time\(\)|datetime\.now\(|date\.today\(", src))
    net = len(re.findall(r"requests\.(get|post)|urllib\.request|socket\.", src))
    tmp = len(re.findall(r"tempfile|TemporaryDirectory|mkdtemp|NamedTemporaryFile", src))
    repo_write = len(re.findall(r"runtime/|ROOT\s*/\s*[\"']runtime|edge_lab\.sqlite3", src))
    subproc = len(re.findall(r"subprocess\.", src))
    low = src.lower()
    for name, pat in critical.items():
        if re.search(pat, low, re.S): crit_hits[name].append(rel)
    r = {"module": rel, "lines": src.count("\n") + 1, "tests": len(tests), "asserts": sum(calls.values()),
         "assertEqual": calls["assertEqual"], "assertTrue_False": calls["assertTrue"] + calls["assertFalse"],
         "assertIn": calls["assertIn"] + calls["assertNotIn"], "assertRaises": calls["assertRaises"] + calls["assertRaisesRegex"],
         "assertAlmostEqual": calls["assertAlmostEqual"], "hex64_literals": hex64, "hex16_literals": hex16,
         "doc_refs": reads_docs, "wall_clock": wall, "network": net, "tempdir_refs": tmp, "repo_runtime_refs": repo_write, "subprocess": subproc}
    rows.append(r)
    for k, v in r.items():
        if isinstance(v, int): tot[k] += v
json.dump({"modules": rows, "totals": tot, "critical_coverage": {k: sorted(set(v)) for k, v in crit_hits.items()}}, open(sys.argv[1], "w"), indent=1)
print("modules", len(rows), "| tests", tot["tests"], "| lines", tot["lines"], "| asserts", tot["asserts"])
print("assert mix: equal", tot["assertEqual"], "true/false", tot["assertTrue_False"], "in", tot["assertIn"], "raises", tot["assertRaises"], "almostEqual", tot["assertAlmostEqual"])
print("hex64 string literals (hash pins):", tot["hex64_literals"], "in", sum(1 for r in rows if r.get("hex64_literals")), "modules | hex16 ids:", tot["hex16_literals"])
print("modules referencing docs:", [r["module"] for r in rows if r.get("doc_refs")])
print("wall-clock refs:", tot["wall_clock"], "| network refs:", tot["network"], [r["module"] for r in rows if r.get("network")], "| subprocess:", [r["module"] for r in rows if r.get("subprocess")])
print("repo runtime/ refs:", [(r["module"], r["repo_runtime_refs"]) for r in rows if r.get("repo_runtime_refs")])
print("\nCRITICAL-SCENARIO KEYWORD COVERAGE (modules whose text matches; presence is not proof of a real test):")
for k in critical:
    v = sorted(set(crit_hits.get(k, [])))
    print(f"  {k:36s} {len(v):3d} modules  {v[:6]}{' ...' if len(v) > 6 else ''}")
