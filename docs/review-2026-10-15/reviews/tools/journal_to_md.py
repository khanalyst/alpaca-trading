"""Persist workflow review/verify results from the journal to Markdown handoff files."""
import json, sys, os, collections
J, OUTDIR = sys.argv[1], sys.argv[2]
reviews, verdicts = {}, collections.defaultdict(lambda: collections.defaultdict(dict))
labels = {}
for line in open(J):
    d = json.loads(line)
    if d.get("type") == "started": labels[d["key"]] = d.get("label", "")
    if d.get("type") == "result":
        lab = labels.get(d["key"], ""); r = d.get("result")
        if r is None: continue
        if lab.startswith("review:"): reviews[r["group"]] = r
        elif lab.startswith("verify:"):
            _, g, lens, _ = lab.split(":")
            for v in r.get("verdicts", []): verdicts[g][v["id"]][lens] = v
for g, r in reviews.items():
    L = [f"# Review handoff: {g}", "", "Source: workflow structured result (reviewer could not write files; this file is generated from the journal).", "",
         "## Files read", "", "| file | lines | read to end |", "|---|---|---|"]
    L += [f"| {f['path']} | {int(f['lines'])} | {'yes' if f['read_to_end'] else 'NO'} |" for f in r["files"]]
    L += ["", "## What this part does (as implemented)", "", r["system_description"], "", "## Findings", ""]
    for f in r["findings"]:
        v = verdicts[g].get(f["id"], {})
        vt = "; ".join(f"{lens}: {x['verdict']} (sev {x['corrected_severity']})" for lens, x in sorted(v.items())) or ("not verified (LOW)" if f["severity"] == "LOW" else "verification pending")
        L += [f"### {f['id']} {f['title']}", f"- Severity: {f['severity']}  |  Category: {f['category']}  |  Confidence: {f['confidence']}",
              f"- Location: `{f['location']}`", f"- Evidence: {f['evidence_quote']}", f"- Why it matters: {f['why_it_matters']}",
              f"- Suggested fix: {f['suggested_fix']}", f"- How verified by reviewer: {f['how_verified']}", f"- Independent verification: {vt}"]
        for lens, x in sorted(v.items()):
            L += [f"  - {lens} verifier evidence: {x['evidence']}", f"  - {lens} verifier reasoning: {x['reasoning']}"]
        L += [""]
    L += ["## Over-engineering", "", r["over_engineering"], "", "## Cross-group questions", ""] + [f"- {q}" for q in r["cross_group_questions"]]
    L += ["", "## Summary", ""] + [f"- {s}" for s in r["summary"]]
    open(os.path.join(OUTDIR, f"handoff-{g}.md"), "w").write("\n".join(L) + "\n")
    print(g, len(r["findings"]), "findings;", sum(1 for f in r["findings"] if f["id"] in verdicts[g]), "with verdicts")
