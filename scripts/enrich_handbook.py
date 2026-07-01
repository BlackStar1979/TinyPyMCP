#!/usr/bin/env python3
"""Enrich handbook technique cards (triggers / anti_triggers / aliases + fill
empty layer/maturity) via OVH gpt-oss, committed to the REVIEW branch rag/enrich
of the handbook repo through the GitHub git-data API (no git binary).

Concurrent (ThreadPool) + retry, because gpt-oss calls are slow and occasionally
time out. Only cards with an empty `triggers:` are (re)enriched. Runs INSIDE the
tinypymcp container:

    curl -sL https://raw.githubusercontent.com/BlackStar1979/TinyPyMCP/main/scripts/enrich_handbook.py -o /work/enrich.py
    PYTHONPATH=/app ENRICH_LIMIT=5 python3 /work/enrich.py     # test
    PYTHONPATH=/app nohup env ... python3 /work/enrich.py &    # full, background
"""
import glob
import io
import json
import os
import re
import tarfile
import tempfile
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from src import ovh_ai_client as oc

OWNER = "BlackStar1979"
REPO = "autonomous-llm-handbook"
BRANCH = "rag/enrich"
API = f"https://api.github.com/repos/{OWNER}/{REPO}"
TOKEN = json.load(open("/secrets/github.json"))["token"]
LIMIT = int(os.environ.get("ENRICH_LIMIT", "0"))
WORKERS = int(os.environ.get("ENRICH_WORKERS", "6"))
MODEL = os.environ.get("ENRICH_MODEL", "gpt-oss-20b")
LOG, DONE = "/tmp/enrich.log", "/tmp/enrich.done"
_loglock = threading.Lock()

SYS = (
    "Jestes ekspertem od inzynierii systemow autonomicznych LLM. Na podstawie karty "
    "techniki wygeneruj metadane retrievalowe. Zwroc WYLACZNIE obiekt JSON o kluczach "
    "aliases, triggers, anti_triggers. aliases: 0-4 inne nazwy/warianty tej techniki. "
    "triggers: 3-6 konkretnych sytuacji/objawow PO POLSKU kiedy tej techniki uzyc "
    "(opisowo, kazdy jako krotka fraza BEZ przecinkow). anti_triggers: 2-4 sytuacje kiedy "
    "NIE stosowac albo typowe mylne dopasowania (bez przecinkow). Zadnego komentarza, sam JSON."
)


def gh(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
        "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _log(msg):
    with _loglock:
        open(LOG, "a").write(msg + "\n")


def sanitize(items):
    out = []
    for it in items or []:
        s = str(it).replace(",", ";").replace('"', "'").strip()
        if s:
            out.append(s)
    return out[:6]


def set_list(text, key, items):
    val = "[" + ", ".join('"' + s + '"' for s in items) + "]" if items else "[]"
    return re.sub(r"^(" + key + r"):\s*\[\s*\]\s*$", lambda m: f"{m.group(1)}: {val}",
                  text, count=1, flags=re.M)


def set_scalar_if_empty(text, key, value):
    if not value:
        return text
    return re.sub(r"^(" + key + r"):\s*$", lambda m: f"{m.group(1)}: {value}",
                  text, count=1, flags=re.M)


def enrich_one(path):
    text = open(path, encoding="utf-8").read()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        return None
    fm, body = m.group(1), m.group(2)
    if not re.search(r"^id: TQ_", fm, re.M) or not re.search(r"^triggers:\s*\[\s*\]\s*$", fm, re.M):
        return None
    tm = re.search(r"^title:\s*(.+)$", fm, re.M)
    title = re.sub(r'^"?Karta(?: techniki)?:\s*', "", tm.group(1).strip("\"'")) if tm else ""
    gen = None
    for _ in range(3):
        r = oc.chat([{"role": "system", "content": SYS},
                     {"role": "user", "content": f"Tytul: {title}\n\n{body[:1800]}"}],
                    model=MODEL, max_tokens=700)
        if r.get("ok") and r.get("content"):
            mj = re.search(r"\{.*\}", r["content"], re.S)
            if mj:
                try:
                    gen = json.loads(mj.group(0))
                    break
                except Exception:
                    pass
    if gen is None:
        _log(f"{os.path.basename(path)}: FAIL (no valid JSON)")
        return None
    new = text
    for k in ("aliases", "triggers", "anti_triggers"):
        new = set_list(new, k, sanitize(gen.get(k)))
    # fill empty layer/maturity from body; clean title prefix
    lm = re.search(r"^\s*Warstwa:\s*(L\d+)", body, re.M | re.I)
    if lm:
        new = set_scalar_if_empty(new, "layer", lm.group(1))
    sm = re.search(r"^\s*Status:\s*([ABCDX])", body, re.M | re.I)
    if sm:
        new = set_scalar_if_empty(new, "maturity", sm.group(1).upper())
    if tm:
        clean = title.replace('"', "'")
        new = re.sub(r"^title:\s*.+$", f'title: "{clean}"', new, count=1, flags=re.M)
    if new == text:
        return None
    rel = "02_TECHNIQUE_CARDS/" + os.path.basename(path)
    sha = gh("POST", f"{API}/git/blobs", {"content": new, "encoding": "utf-8"})["sha"]
    _log(f"{rel}: {len(sanitize(gen.get('triggers')))}t "
         f"{len(sanitize(gen.get('anti_triggers')))}a {len(sanitize(gen.get('aliases')))}al")
    return {"path": rel, "mode": "100644", "type": "blob", "sha": sha}


def main():
    open(LOG, "w").write("")
    base_sha = gh("GET", f"{API}/git/ref/heads/main")["object"]["sha"]
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
    files = [p for p in sorted(glob.glob(os.path.join(root, "02_TECHNIQUE_CARDS", "*.md")))
             if os.path.basename(p) != "000_CARD_TEMPLATE.md"]
    if LIMIT:
        files = files[:LIMIT]
    entries = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for e in ex.map(enrich_one, files):
            if e:
                entries.append(e)
    if not entries:
        print("nothing enriched")
        return
    tree = gh("POST", f"{API}/git/trees", {"base_tree": base_tree, "tree": entries})
    commit = gh("POST", f"{API}/git/commits", {
        "message": f"Enrich {len(entries)} technique cards: triggers/anti_triggers/aliases "
                   f"+ layer/maturity fill (gpt-oss auto, REVIEW before merge)",
        "tree": tree["sha"], "parents": [base_sha]})
    try:
        gh("POST", f"{API}/git/refs", {"ref": f"refs/heads/{BRANCH}", "sha": commit["sha"]})
    except Exception:
        gh("PATCH", f"{API}/git/refs/heads/{BRANCH}", {"sha": commit["sha"], "force": True})
    res = {"enriched": len(entries), "attempted": len(files), "branch": BRANCH,
           "commit": commit["sha"][:8]}
    open(DONE, "w").write(json.dumps(res))
    print("DONE", json.dumps(res))


if __name__ == "__main__":
    main()
