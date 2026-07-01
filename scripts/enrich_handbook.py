#!/usr/bin/env python3
"""Enrich handbook technique cards with retrieval frontmatter (triggers /
anti_triggers / aliases) via the OVH gpt-oss model, and commit the result to a
REVIEW branch of the handbook repo through the GitHub git-data (Trees) API — no
git binary required.

Runs INSIDE the tinypymcp container (needs `from src import ovh_ai_client` and
/secrets/github.json). Fetch it into the container from the public TinyPyMCP repo:

    curl -sL https://raw.githubusercontent.com/BlackStar1979/TinyPyMCP/main/scripts/enrich_handbook.py -o /work/enrich.py
    PYTHONPATH=/app ENRICH_LIMIT=5 python3 /work/enrich.py     # test on 5 cards
    PYTHONPATH=/app python3 /work/enrich.py                    # all remaining

Only cards whose `triggers:` is still empty are enriched (idempotent-ish; already
enriched / manually curated cards are skipped). Items are comma-sanitized so they
stay parseable by handbook_rag's minimal inline-list parser.
"""
import glob
import io
import json
import os
import re
import tarfile
import tempfile
import urllib.request

from src import ovh_ai_client as oc

OWNER = "BlackStar1979"
REPO = "autonomous-llm-handbook"
BRANCH = "rag/enrich"
API = f"https://api.github.com/repos/{OWNER}/{REPO}"
TOKEN = json.load(open("/secrets/github.json"))["token"]
LIMIT = int(os.environ.get("ENRICH_LIMIT", "0"))  # 0 = all remaining
LOG = "/tmp/enrich.log"
DONE = "/tmp/enrich.done"

SYS = (
    "Jestes ekspertem od inzynierii systemow autonomicznych LLM. Na podstawie karty "
    "techniki wygeneruj metadane retrievalowe. Zwroc WYLACZNIE obiekt JSON o kluczach "
    "aliases, triggers, anti_triggers. aliases: 0-4 inne nazwy/warianty tej techniki. "
    "triggers: 3-6 konkretnych sytuacji/objawow PO POLSKU, kiedy tej techniki uzyc "
    "(opisowo, kazdy jako krotka fraza BEZ przecinkow w srodku). anti_triggers: 2-4 "
    "sytuacje kiedy NIE stosowac albo typowe mylne dopasowania (bez przecinkow w srodku). "
    "Zadnego komentarza, sam JSON."
)


def gh(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def sanitize(items):
    out = []
    for it in items or []:
        s = str(it).replace(",", ";").replace('"', "'").strip()
        if s:
            out.append(s)
    return out[:6]


def inline(key, items):
    val = "[" + ", ".join('"' + s + '"' for s in items) + "]"
    return val if items else "[]"


def set_list(text, key, items):
    """Replace an empty `key: []` line with a populated inline list (once)."""
    return re.sub(r"^(" + key + r"):\s*\[\s*\]\s*$",
                  lambda m: f"{m.group(1)}: {inline(key, items)}", text, count=1, flags=re.M)


def main():
    open(LOG, "w").write("")
    ref = gh("GET", f"{API}/git/ref/heads/main")
    base_sha = ref["object"]["sha"]
    base_tree = gh("GET", f"{API}/git/commits/{base_sha}")["tree"]["sha"]

    raw = urllib.request.urlopen(urllib.request.Request(
        f"{API}/tarball/main", headers={"Authorization": f"Bearer {TOKEN}"}), timeout=90).read()
    d = tempfile.mkdtemp()
    with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
        try:
            tf.extractall(d, filter="data")
        except TypeError:
            tf.extractall(d)
    root = next(os.path.join(d, x) for x in os.listdir(d) if os.path.isdir(os.path.join(d, x)))

    entries = []
    done = skipped = 0
    for p in sorted(glob.glob(os.path.join(root, "02_TECHNIQUE_CARDS", "*.md"))):
        if os.path.basename(p) == "000_CARD_TEMPLATE.md":
            continue
        text = open(p, encoding="utf-8").read()
        fm = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        fm = fm.group(1) if fm else ""
        if not re.search(r"^id: TQ_", fm, re.M):
            continue
        if not re.search(r"^triggers:\s*\[\s*\]\s*$", fm, re.M):
            skipped += 1
            continue
        if LIMIT and done >= LIMIT:
            break
        tm = re.search(r"^title:\s*(.+)$", fm, re.M)
        title = tm.group(1).strip("\"'") if tm else ""
        body = text.split("---\n", 2)[-1][:1800]
        r = oc.chat([{"role": "system", "content": SYS},
                     {"role": "user", "content": f"Tytul: {title}\n\n{body}"}],
                    model="gpt-oss-20b", max_tokens=750)
        if not r.get("ok") or not r.get("content"):
            skipped += 1
            continue
        mj = re.search(r"\{.*\}", r["content"], re.S)
        if not mj:
            skipped += 1
            continue
        try:
            gen = json.loads(mj.group(0))
        except Exception:
            skipped += 1
            continue
        new = text
        for k in ("aliases", "triggers", "anti_triggers"):
            new = set_list(new, k, sanitize(gen.get(k)))
        if new == text:
            skipped += 1
            continue
        rel = "02_TECHNIQUE_CARDS/" + os.path.basename(p)
        blob = gh("POST", f"{API}/git/blobs", {"content": new, "encoding": "utf-8"})
        entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        done += 1
        open(LOG, "a").write(f"{rel}: {len(sanitize(gen.get('triggers')))}t "
                             f"{len(sanitize(gen.get('anti_triggers')))}a "
                             f"{len(sanitize(gen.get('aliases')))}al\n")

    if not entries:
        print("nothing to enrich; skipped", skipped)
        return
    tree = gh("POST", f"{API}/git/trees", {"base_tree": base_tree, "tree": entries})
    commit = gh("POST", f"{API}/git/commits", {
        "message": f"Enrich {done} technique cards: triggers/anti_triggers/aliases "
                   f"(gpt-oss auto, REVIEW before merge)",
        "tree": tree["sha"], "parents": [base_sha]})
    try:
        gh("POST", f"{API}/git/refs", {"ref": f"refs/heads/{BRANCH}", "sha": commit["sha"]})
    except Exception:
        gh("PATCH", f"{API}/git/refs/heads/{BRANCH}", {"sha": commit["sha"], "force": True})
    res = {"enriched": done, "skipped": skipped, "branch": BRANCH, "commit": commit["sha"][:8]}
    open(DONE, "w").write(json.dumps(res))
    print("DONE", json.dumps(res))


if __name__ == "__main__":
    main()
