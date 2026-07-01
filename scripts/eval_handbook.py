#!/usr/bin/env python3
"""Handbook RAG eval harness. For each technique card, generate one INTENT query
(describe the problem/situation where the card is the answer, no jargon/name),
then measure whether handbook_rag.search retrieves that card. Runs the same query
set against two stores (un-enriched live vs enriched branch) for a fair A/B.

Metrics: top1 (expected is rank 1), recall@5 (expected in top 5), MRR@5.

Runs INSIDE the tinypymcp container:
    curl -sL https://raw.githubusercontent.com/BlackStar1979/TinyPyMCP/main/scripts/eval_handbook.py -o /work/eval.py
    PYTHONPATH=/app python3 /work/eval.py       # generates queries (cached) + A/B
Query cache: /work/eval_queries.jsonl (delete to regenerate).
"""
import io
import json
import os
import tarfile
import tempfile
import urllib.request
import math
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from src import handbook_rag as h, ovh_ai_client as oc

TOKEN = json.load(open("/secrets/github.json"))["token"]
API = "https://api.github.com/repos/BlackStar1979/autonomous-llm-handbook"
QF = "/work/eval_queries.jsonl"
MODEL = os.environ.get("EVAL_MODEL", "Mistral-Small-3.2-24B-Instruct-2506")
WORKERS = int(os.environ.get("EVAL_WORKERS", "6"))

GENSYS = ("Napisz JEDNO krotkie pytanie po polsku opisujace problem lub sytuacje, w "
          "ktorej ta technika jest wlasciwym rozwiazaniem. NIE uzywaj nazwy techniki ani "
          "zargonu z karty. Zwroc samo pytanie, bez cudzyslowow i bez komentarza.")


def eb(texts):
    out = []
    for i in range(0, len(texts), 25):
        ch = list(texts[i:i + 25])
        r = oc.embeddings(ch)
        vs = r.get("embeddings") if r.get("ok") else None
        if not vs or len(vs) != len(ch):
            out += [None] * len(ch)
            continue
        for v in vs:
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
    return out


def pull(ref):
    raw = urllib.request.urlopen(urllib.request.Request(
        f"{API}/tarball/{ref}", headers={"Authorization": f"Bearer {TOKEN}"}), timeout=90).read()
    d = tempfile.mkdtemp()
    with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
        try:
            tf.extractall(d, filter="data")
        except TypeError:
            tf.extractall(d)
    root = next(os.path.join(d, x) for x in os.listdir(d) if os.path.isdir(os.path.join(d, x)))
    return h.cards_from_dir(os.path.join(root, "02_TECHNIQUE_CARDS"))


def gen_q(card):
    for _ in range(3):
        r = oc.chat([{"role": "system", "content": GENSYS},
                     {"role": "user", "content": f"Tytul: {card['title']}\n\n{card['problem'][:700]}"}],
                    model=MODEL, max_tokens=120)
        if r.get("ok") and r.get("content"):
            q = r["content"].strip().strip('"').split("\n")[0].strip()
            if len(q) > 10:
                return {"slug": card["slug"], "query": q}
    return None


def get_queries(cards):
    if os.path.exists(QF):
        return [json.loads(l) for l in open(QF, encoding="utf-8") if l.strip()]
    qs = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for r in ex.map(gen_q, cards):
            if r:
                qs.append(r)
    with open(QF, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(q, ensure_ascii=False) for q in qs))
    return qs


def eval_store(db, queries, label):
    h.DB_PATH = Path(db)
    top1 = rec5 = 0
    mrr = 0.0
    for q in queries:
        rs = h.search(q["query"], eb, top_k=5)
        slugs = [x["slug"] for x in rs["results"]]
        if q["slug"] in slugs:
            rk = slugs.index(q["slug"])
            rec5 += 1
            mrr += 1.0 / (rk + 1)
            if rk == 0:
                top1 += 1
    n = len(queries) or 1
    print(f"{label:28s} top1={top1/n:.3f}  recall@5={rec5/n:.3f}  mrr@5={mrr/n:.3f}  "
          f"(n={len(queries)}, miss={len(queries)-rec5})")


def main():
    cards_main = pull("main")
    queries = get_queries(cards_main)
    print("eval queries:", len(queries), "| model:", MODEL)
    eval_store("/data/handbook_rag.db", queries, "UN-ENRICHED (main/live)")
    enr = pull("rag/enrich")
    h.DB_PATH = Path("/tmp/eval_enriched.db")
    h.ingest(enr, eb, prune_missing=True)
    eval_store("/tmp/eval_enriched.db", queries, "ENRICHED (rag/enrich)")
    open("/tmp/eval.done", "w").write("ok")
    print("EVAL-DONE")


if __name__ == "__main__":
    main()
