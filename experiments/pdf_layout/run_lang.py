#!/usr/bin/env python3
"""Language identification + document-quality flags, with a benchmark.

    python experiments/pdf_layout/run_lang.py                      # sample+live, 120 docs/entity
    python experiments/pdf_layout/run_lang.py --per-entity 400      # more data, still seconds
    python experiments/pdf_layout/run_lang.py --all-live            # every live PDF (flag census)
    python experiments/pdf_layout/run_lang.py --bench-only          # skip the census

Ground truth is the entity's country from `masters/entities.csv`, restricted to the
unambiguous countries (ES/PT/FR/IT/DE/NL/GB/US/AU/PL/BR/MX). Multilingual entities
(CH, MA, CN) are reported but not scored: their documents genuinely mix languages, so
"agreeing with the country" is not correctness.

Outputs: `out/lang_bench.json`, `out/quality_flags.jsonl`, and a printed report.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "experiments"))

import polars as pl  # noqa: E402
from pypdf import PdfReader  # noqa: E402

from pdf_layout.lang import (  # noqa: E402
    AMBIGUOUS_COUNTRIES,
    COUNTRY_LANG,
    CentroidDetector,
    GatedFusion,
    SklearnKnnDetector,
    LibraryDetector,
    NaiveBayesDetector,
    StopwordDetector,
    quality,
)

DEFAULT_OUT = Path(__file__).resolve().parent / "out"


# ------------------------------------------------------------------ loading


def _text_of(path: str) -> str:
    try:
        return "\n".join((page.extract_text() or "") for page in PdfReader(path).pages)
    except Exception:
        return ""


def load_docs(phases: list[str], data_dir: Path, per_entity: int) -> list[dict]:
    countries = {}
    entities_csv = data_dir / "masters" / "entities.csv"
    if entities_csv.exists():
        for row in pl.read_csv(entities_csv, infer_schema_length=0).to_dicts():
            countries[row["entity"]] = (row.get("country") or "").strip().upper()

    by_entity: dict[str, list[dict]] = defaultdict(list)
    for phase in phases:
        index = data_dir / phase / "pdfs" / "index.csv"
        if not index.exists():
            continue
        for row in pl.read_csv(index, infer_schema_length=0).to_dicts():
            entity = row["file"].split("/")[0]
            by_entity[entity].append(
                {
                    "path": str(data_dir / phase / "pdfs" / row["file"]),
                    "entity": entity,
                    "phase": phase,
                    "document_type": row["document_type"],
                    "number": row["document_number"],
                    "date": row["document_date"],
                }
            )

    docs: list[dict] = []
    for entity, rows in sorted(by_entity.items()):
        rows.sort(key=lambda r: r["date"])
        sampled = rows if per_entity <= 0 else rows[:per_entity]
        for row in sampled:
            row["country"] = countries.get(entity, "")
            truth = COUNTRY_LANG.get(row["country"])
            row["truth"] = truth if row["country"] not in AMBIGUOUS_COUNTRIES else None
            docs.append(row)
    return docs


def extract_texts(docs: list[dict], workers: int) -> None:
    started = time.time()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_text_of, d["path"]): d for d in docs}
        for i, future in enumerate(as_completed(futures), 1):
            futures[future]["text"] = future.result()
            if i % 500 == 0:
                print(f"  text {i:,}/{len(docs):,}  {i/(time.time()-started):,.0f}/s", file=sys.stderr)
    print(f"  extracted {len(docs):,} texts in {time.time()-started:.1f}s", file=sys.stderr)


# -------------------------------------------------------------- benchmarking


def prf(gold: list[str], pred: list[str], label: str) -> tuple[float, float, float]:
    tp = sum(1 for g, p in zip(gold, pred) if g == label and p == label)
    fp = sum(1 for g, p in zip(gold, pred) if g != label and p == label)
    fn = sum(1 for g, p in zip(gold, pred) if g == label and p != label)
    p_ = tp / (tp + fp) if tp + fp else 0.0
    r_ = tp / (tp + fn) if tp + fn else 0.0
    f_ = 2 * p_ * r_ / (p_ + r_) if p_ + r_ else 0.0
    return p_, r_, f_


def benchmark(fit: list[dict], val: list[dict], test: list[dict]) -> dict:
    """Fit on `fit`, tune the gate on `val`, report on `test` (three disjoint splits)."""
    x_fit, y_fit = [d["text"] for d in fit], [d["truth"] for d in fit]
    x_val, y_val = [d["text"] for d in val], [d["truth"] for d in val]

    # drop documents with no text: no method can classify them, they are *flagged* instead
    keep = [i for i, text in enumerate(d["text"] for d in test) if len(text.strip()) >= 40]
    x_test = [test[i]["text"] for i in keep]
    y_test = [test[i]["truth"] for i in keep]
    labels = sorted(set(y_fit))
    known = set(labels)

    def accuracy(detector, texts, gold) -> float:
        return sum(detector.predict(t)[0] == g for t, g in zip(texts, gold)) / max(len(gold), 1)

    def calibrate_temperature(detector, texts, gold) -> float:
        """Fit the softmax temperature so `confidence` means something (NLL on validation)."""
        import math as _math

        best_t, best_nll = detector.temperature, float("inf")
        for temperature in (0.005, 0.01, 0.02, 0.03, 0.05, 0.08, 0.12, 0.2, 0.3):
            detector.temperature = temperature
            nll = 0.0
            for text, label in zip(texts, gold):
                pred, conf = detector.predict(text)
                nll -= _math.log(max(conf if pred == label else (1 - conf) / 7, 1e-6))
            if nll < best_nll:
                best_t, best_nll = temperature, nll
        detector.temperature = best_t
        return best_t

    base = {
        "stopwords": StopwordDetector(),
        "centroid": CentroidDetector(k=0),
        "naive_bayes": NaiveBayesDetector(),
        # same kNN, four implementations: tree, tree, brute, and the pure-python reference
        "knn_ball": SklearnKnnDetector(k=5, algorithm="ball_tree"),
        "knn_kd": SklearnKnnDetector(k=5, algorithm="kd_tree"),
        "knn_brute": SklearnKnnDetector(k=5, algorithm="brute"),
        # trees only have a chance after a dense projection (sparse input falls back to brute)
        "knn_ball_svd": SklearnKnnDetector(k=5, algorithm="ball_tree", reduce_dims=64),
        "knn_kd_svd": SklearnKnnDetector(k=5, algorithm="kd_tree", reduce_dims=64),
        "knn_py": CentroidDetector(k=5),
        "langdetect": LibraryDetector(),
    }
    # kNN is O(train docs) per query: cap it per language so the benchmark stays usable
    knn_fit, seen = [], Counter()
    for doc in fit:
        if seen[doc["truth"]] < 300:
            knn_fit.append(doc)
            seen[doc["truth"]] += 1

    fitted = {}
    for name, detector in base.items():
        docs_for_fit = knn_fit if name == "knn_py" else fit
        started = time.time()
        detector.fit([d["text"] for d in docs_for_fit], [d["truth"] for d in docs_for_fit])
        fitted[name] = detector
        print(f"  fit {name:<12} {len(docs_for_fit):>6,} docs  {time.time()-started:5.2f}s", file=sys.stderr)
    report_temperature = calibrate_temperature(fitted["centroid"], x_val, y_val)

    # secondary voters weighted by how well they did on the validation split
    secondary = [fitted["naive_bayes"], fitted["stopwords"], fitted["langdetect"]]
    weights = [max(accuracy(d, x_val, y_val) - 0.5, 0.01) for d in secondary]

    # gate: below which margin do we consult the secondary voters?
    best_threshold, best_score = 0.0, -1.0
    for threshold in (0.0, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95):
        fusion = GatedFusion(fitted["centroid"], secondary, threshold=threshold, weights=weights, labels=known)
        fusion.fit(x_fit, y_fit)
        score = accuracy(fusion, x_val, y_val)
        if score > best_score:
            best_threshold, best_score = threshold, score
    fusion = GatedFusion(fitted["centroid"], secondary, threshold=best_threshold, weights=weights, labels=known)
    fusion.fit(x_fit, y_fit)

    methods = dict(fitted)
    methods["gated"] = fusion

    results = []
    for name, detector in methods.items():
        started = time.time()
        predictions = [detector.predict(text) for text in x_test]
        print(f"  eval {name:<12} {len(x_test):>6,} docs  {time.time()-started:5.2f}s", file=sys.stderr)
        elapsed = time.time() - started
        preds = [p[0] for p in predictions]
        confs = [p[1] for p in predictions]
        p_, r_, f_ = zip(*(prf(y_test, preds, label) for label in labels))
        accuracy_value = sum(g == p for g, p in zip(y_test, preds)) / max(len(y_test), 1)
        high = [g == p for g, p, c in zip(y_test, preds, confs) if c >= 0.5]
        low = [g == p for g, p, c in zip(y_test, preds, confs) if c < 0.5]
        results.append(
            {
                "method": name,
                "accuracy": round(accuracy_value, 4),
                "macro_f1": round(sum(f_) / len(f_), 4),
                "per_language": {
                    label: {"p": round(pp, 3), "r": round(rr, 3), "f1": round(ff, 3)}
                    for label, pp, rr, ff in zip(labels, p_, r_, f_)
                },
                "ms_per_doc": round(elapsed / max(len(x_test), 1) * 1000, 3),
                "conf_ge_0.5": {"n": len(high), "accuracy": round(sum(high) / len(high), 3) if high else None},
                "conf_lt_0.5": {"n": len(low), "accuracy": round(sum(low) / len(low), 3) if low else None},
                "confusion": _confusion(y_test, preds),
            }
        )
    return {
        "results": results,
        "test_docs": len(y_test),
        "train_docs": len(x_fit),
        "val_docs": len(x_val),
        "gate_threshold": best_threshold,
        "centroid_temperature": report_temperature,
        "secondary_weights": [round(w, 3) for w in weights],
        "languages": {lang: sum(1 for t in y_test if t == lang) for lang in labels},
    }


def _confusion(gold: list[str], pred: list[str]) -> dict[str, dict[str, int]]:
    table: dict[str, Counter] = defaultdict(Counter)
    for g, p in zip(gold, pred):
        table[g][p] += 1
    return {g: dict(sorted(row.items())) for g, row in sorted(table.items())}


def tune_fusion(train: list[dict]) -> list[float]:
    """Small grid over three weights; the split is by document, so this is not leakage."""
    best, best_score = (0.45, 0.30, 0.25), -1.0
    x = [d["text"] for d in train]
    y = [d["truth"] for d in train]
    for w_stop in (0.2, 0.3, 0.4):
        for w_knn in (0.3, 0.4, 0.5):
            w_lib = round(1 - w_stop - w_knn, 2)
            if w_lib < 0.1:
                continue
            fusion = Fusion([StopwordDetector(), CentroidDetector(k=0), LibraryDetector()], [w_stop, w_knn, w_lib], set(y))
            fusion.fit(x, y)
            score = sum(fusion.predict(text)[0] == label for text, label in zip(x, y)) / len(x)
            if score > best_score:
                best, best_score = (w_stop, w_knn, w_lib), score
    return list(best)


# ------------------------------------------------------------------- reporting


def census(docs: list[dict]) -> dict:
    """No ground truth needed: language mix and quality flags over the whole phase."""
    fused = GatedFusion(CentroidDetector(k=0), [NaiveBayesDetector(), LibraryDetector()], threshold=0.6, weights=[0.3, 0.2], labels=set(COUNTRY_LANG.values()))
    labelled = [d for d in docs if d.get("truth")]
    if labelled:
        fused.fit([d["text"] for d in labelled], [d["truth"] for d in labelled])

    by_entity: dict[str, Counter] = defaultdict(Counter)
    low_conf: Counter = Counter()
    flags = Counter()
    chars: list[int] = []
    flagged: list[dict] = []
    for d in docs:
        q = quality(d["text"])
        chars.append(q.chars)
        for flag in q.flags:
            flags[flag] += 1
        lang, conf = fused.predict(d["text"]) if labelled else ("unknown", 0.0)
        by_entity[d["entity"]][lang] += 1
        if conf < 0.6:
            low_conf[d["entity"]] += 1
        if q.flags:
            flagged.append({"path": d["path"], "entity": d["entity"], "chars": q.chars,
                            "flags": q.flags, "language": lang, "confidence": conf})

    return {
        "documents": len(docs),
        "chars": {"min": min(chars) if chars else 0, "p10": _pct(chars, 10), "median": _pct(chars, 50),
                  "p90": _pct(chars, 90), "max": max(chars) if chars else 0},
        "flags": dict(flags),
        "flagged": flagged,
        "language_by_entity": {e: dict(c.most_common(4)) for e, c in sorted(by_entity.items())},
        "low_confidence": dict(low_conf),
    }


def _pct(values: list[int], p: int) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p / 100))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="language ID + document quality, benchmarked")
    parser.add_argument("--phase", action="append", choices=["sample", "train-lite", "train", "live", "final"])
    parser.add_argument("--data", type=Path, default=ROOT / "data")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--per-entity", type=int, default=120, help="documents per entity for the benchmark")
    parser.add_argument("--all-live", action="store_true", help="census over every live PDF instead of sampling")
    parser.add_argument("--bench-only", action="store_true")
    parser.add_argument("--census-only", action="store_true", help="skip the benchmark (fast flag census)")
    parser.add_argument("--workers", type=int, default=min(os.cpu_count() or 1, 8))
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)

    random.seed(args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    phases = args.phase or ["sample", "live"]
    per_entity = 0 if args.all_live else args.per_entity

    docs = load_docs(phases, args.data, per_entity)
    if not docs:
        print("no PDFs found: check data/<phase>/pdfs/index.csv", file=sys.stderr)
        return 1
    print(f"loaded {len(docs):,} documents across {len({d['entity'] for d in docs})} entities", file=sys.stderr)
    extract_texts(docs, args.workers)

    scored = [d for d in docs if d.get("truth")]
    by_lang: dict[str, list[dict]] = defaultdict(list)
    for d in scored:
        by_lang[d["truth"]].append(d)
    fit, val, test = [], [], []
    for lang, items in by_lang.items():
        random.shuffle(items)
        n = len(items)
        cut1, cut2 = max(1, int(n * 0.6)), max(2, int(n * 0.8))
        fit += items[:cut1]
        val += items[cut1:cut2]
        test += items[cut2:]
    print(
        f"benchmark: {len(fit):,} fit / {len(val):,} val / {len(test):,} test over {len(by_lang)} languages",
        file=sys.stderr,
    )

    report = {"results": [], "languages": {}, "gate_threshold": 0, "secondary_weights": [],
              "centroid_temperature": 0, "test_docs": 0, "train_docs": 0, "val_docs": 0}
    if not args.census_only:
        report = benchmark(fit, val, test)

    lines = [
        f"documents {len(docs):,}  |  scored {len(scored):,}  |  fit {len(fit):,} / val {len(val):,} / test {len(test):,}",
        f"gate threshold = {report['gate_threshold']}  |  centroid temperature = {report['centroid_temperature']}  |  "
        f"secondary weights (nb, stopwords, langdetect) = {report['secondary_weights']}",
        "",
    ]
    lines.append(f"{'method':<12}{'accuracy':>9}{'macro-F1':>10}{'ms/doc':>9}   conf>=0.5")
    for row in report["results"]:
        conf = row["conf_ge_0.5"]
        lines.append(
            f"{row['method']:<12}{row['accuracy']:>9.1%}{row['macro_f1']:>10.3f}{row['ms_per_doc']:>9.2f}   "
            f"{conf['accuracy'] if conf['accuracy'] is not None else 0:.1%} on {conf['n']:,} docs"
        )
    if report["results"]:
        lines.append("")
        lines.append("per language (gated):")
        gated = next((r for r in report["results"] if r["method"] == "gated"), report["results"][0])
        for lang, scores in gated["per_language"].items():
            lines.append(f"  {lang:<4} n={report['languages'].get(lang, 0):>4}  P={scores['p']:.2f}  R={scores['r']:.2f}  F1={scores['f1']:.2f}")
        lines.append("")
        lines.append("confusion (gated, rows = truth):")
        for gold, row in gated["confusion"].items():
            cells = "  ".join(f"{pred}:{n}" for pred, n in row.items())
            lines.append(f"  {gold:<4} {cells}")

    if not args.bench_only:
        census_docs = docs if args.all_live else scored
        census_report = census(census_docs)
        report["census"] = {k: v for k, v in census_report.items() if k != "flagged"}
        lines += ["", f"quality census ({census_report['documents']:,} documents):",
                  "  chars " + json.dumps(census_report["chars"]),
                  "  flags " + json.dumps(census_report["flags"]),
                  "  language mix per entity:"]
        for entity, mix in census_report["language_by_entity"].items():
            top = ", ".join(f"{lang} {n}" for lang, n in list(mix.items())[:3])
            low = census_report["low_confidence"].get(entity, 0)
            lines.append(f"    {entity:<6} {top}" + (f"   (low confidence: {low})" if low else ""))
        with (args.out / "quality_flags.jsonl").open("w") as fh:
            for row in census_report["flagged"]:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        lines.append(f"  flagged documents: {len(census_report['flagged']):,} -> out/quality_flags.jsonl")

    text = "\n".join(lines)
    if report["results"]:  # `--census-only` must not clobber the last benchmark
        (args.out / "lang_bench.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    (args.out / "lang_report.txt").write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
