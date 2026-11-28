#!/usr/bin/env python3
"""Make the report's result charts and tables from the experiment results (Member 3).

Usage (from the repo root):
    pip install matplotlib
    python3 docs/charts/make_charts.py                       # reads experiments/results/
    python3 docs/charts/make_charts.py --results PATH --out docs/figures

Expected input, one folder per run (see docs/charts/README.md for the full format):
    experiments/results/<kind>-<target>_<pattern>_r<repeat>/   e.g. hpa-70_spike_r1, custom-15_ramp_r2
        meta.json        optional: {"kind": "hpa"|"custom", "target": 70, "pattern": "spike", "repeat": 1}
        k6-summary.json  k6 JSON summary (--summary-export file, or handleSummary data, or k6 stdout with it)
        pods.csv         header t,desired,ready ; t = seconds since the k6 test started, one row every ~5 s

Output (in --out):
    fig2_tradeoff.png     average pods vs p95 response time, one panel per pattern, error bars over repeats
    fig3_spike_pods.png   request rate and pods ready over time under the spike pattern
    runs.csv              one row per run
    summary.csv           mean and standard deviation per setting and pattern
    table2_spike.md       the report's Table 2, ready to paste
"""
import argparse
import csv
import json
import math
import re
import statistics
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

PATTERNS = ["ramp", "spike", "periodic"]

# Same stages as load/load-test.js (ramping-arrival-rate, startRate 5). Used only if that file can't be read.
FALLBACK_STAGES = {
    "ramp": [(60, 5), (300, 40), (120, 40), (120, 5)],
    "spike": [(60, 5), (10, 40), (180, 40), (10, 5), (180, 5)],
    "periodic": [(60, 5), (60, 35), (60, 35), (60, 5), (60, 5), (60, 35), (60, 35), (60, 5)],
}
START_RATE = 5

# Colour = kind of autoscaler (reference palette slots 1-2, light mode); marker shape = the setting's target.
HPA_COLOR, CUSTOM_COLOR = "#257de0", "#f47230"
HPA_MARKERS, CUSTOM_MARKERS = ["o", "^", "D", "v", "P"], ["s", "p", "X", "h", "*"]
INK, QUIET, GRID = "#191919", "#666666", "#e6e6e6"


# ---------------------------------------------------------------- reading inputs
def parse_duration(s):
    total = 0
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)", s):
        total += float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return total


def load_stages(repo_root):
    """Read the traffic patterns from load/load-test.js so the charts stay in step with Member 1's file."""
    js = repo_root / "load" / "load-test.js"
    try:
        text = js.read_text()
        stages = {}
        for name in PATTERNS:
            block = re.search(name + r"\s*:\s*\[(.*?)\]", text, re.S).group(1)
            items = re.findall(r"duration\s*:\s*'([^']+)'\s*,\s*target\s*:\s*(\d+)", block)
            stages[name] = [(parse_duration(d), int(t)) for d, t in items]
            if not stages[name]:
                raise ValueError(name)
        start = re.search(r"startRate\s*:\s*(\d+)", text)
        return stages, int(start.group(1)) if start else START_RATE, f"read from {js}"
    except Exception:
        return FALLBACK_STAGES, START_RATE, "built-in copy (load/load-test.js not found or changed format)"


def rate_at(stages, start_rate, t):
    """Requests per second the pattern asks for at time t (linear between stage targets, like k6)."""
    t0, r0 = 0.0, start_rate
    for dur, target in stages:
        if t <= t0 + dur:
            return r0 + (target - r0) * ((t - t0) / dur if dur else 1)
        t0, r0 = t0 + dur, target
    return r0


def read_k6(path):
    text = path.read_text()
    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        marker = "@@K6_SUMMARY_JSON@@"
        if marker in text:
            data = json.loads(text.split(marker, 1)[1].strip().splitlines()[0])
        else:
            for line in reversed(text.splitlines()):
                if line.strip().startswith("{") and '"metrics"' in line:
                    data = json.loads(line)
                    break
    if not data or "metrics" not in data:
        raise ValueError("no k6 JSON summary found")
    m = data["metrics"]

    def vals(name):
        v = m[name]
        return v.get("values", v)  # handleSummary format nests under "values"; --summary-export does not

    dur = vals("http_req_duration")
    failed = vals("http_req_failed")
    reqs = vals("http_reqs")
    return {
        "p95_ms": float(dur["p(95)"]),
        "avg_ms": float(dur["avg"]),
        "failed_pct": 100 * float(failed.get("rate", failed.get("value", 0.0))),
        "requests": int(reqs["count"]),
    }


def read_pods(path):
    rows = []
    with path.open() as f:
        reader = csv.DictReader(f)
        cols = [c.strip().lower() for c in reader.fieldnames or []]
        tcol = next((c for c in ("t", "time", "seconds") if c in cols), None)
        rcol = next((c for c in ("ready", "pods", "replicas") if c in cols), None)
        dcol = "desired" if "desired" in cols else None
        if tcol is None or rcol is None:
            raise ValueError(f"pods.csv needs columns t and ready (has {cols})")
        for raw in reader:
            row = {k.strip().lower(): v for k, v in raw.items()}
            rows.append((float(row[tcol]), int(float(row[rcol])), int(float(row[dcol])) if dcol else None))
    rows.sort()
    return rows


def run_identity(folder):
    meta_path = folder / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    m = re.fullmatch(r"(hpa|custom)-(\d+(?:\.\d+)?)_(ramp|spike|periodic)_r(\d+)", folder.name)
    kind = meta.get("kind") or (m and m.group(1))
    target = meta.get("target") or (m and float(m.group(2)))
    pattern = meta.get("pattern") or (m and m.group(3))
    repeat = meta.get("repeat") or (m and int(m.group(4)))
    if not (kind and target and pattern and repeat):
        raise ValueError("can't tell setting/pattern/repeat: add meta.json or use the folder name format")
    target = float(target)
    tgt = int(target) if target.is_integer() else target
    label = f"HPA {tgt}%" if kind == "hpa" else f"Custom {tgt} req/s"
    return {"kind": kind, "target": target, "pattern": pattern, "repeat": int(repeat), "setting": label,
            "fake": bool(meta.get("fake"))}


# ---------------------------------------------------------------- per-run measures
def pattern_length(stages):
    return sum(d for d, _ in stages)


def spike_reaction(pods, stages):
    """Seconds from the spike's start until pods ready first reach the most pods asked for during the peak."""
    rise_start = stages[0][0]
    peak_end = rise_start + stages[1][0] + stages[2][0]
    if any(d is None for _, _, d in pods):
        return None, "no 'desired' column"
    window = [(t, r, d) for t, r, d in pods if rise_start <= t <= peak_end]
    if not window:
        return None, "no samples in the peak"
    before = [d for t, r, d in pods if t <= rise_start]
    start_pods = before[-1] if before else window[0][2]
    peak_desired = max(d for _, _, d in window)
    if peak_desired <= start_pods:
        return None, "did not scale up"
    for t, r, _ in window:
        if r >= peak_desired:
            return t - rise_start, ""
    return None, "not reached during the peak"


def mean_sd(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return None, None
    return statistics.mean(xs), (statistics.stdev(xs) if len(xs) > 1 else 0.0)


def fmt(m, s, digits=0):
    if m is None:
        return "n/a"
    return f"{m:.{digits}f} ± {s:.{digits}f}"


# ---------------------------------------------------------------- charts
def setting_order(runs):
    seen = {(r["kind"], r["target"], r["setting"]) for r in runs}
    return [s for _, _, s in sorted(seen, key=lambda k: (k[0] != "hpa", k[1]))]


def style_axes(ax):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(QUIET)
    ax.tick_params(colors=QUIET, labelsize=8)
    ax.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def watermark(fig):
    fig.text(0.5, 0.5, "TEST DATA - NOT RESULTS", fontsize=34, color="#ec4647", alpha=0.25,
             ha="center", va="center", rotation=20, weight="bold")


def setting_styles(summary_or_runs, order):
    """Fixed look per setting: colour by kind, marker by position within its kind (follows the setting, not rank)."""
    kinds = {r["setting"]: r["kind"] for r in summary_or_runs}
    styles, n = {}, {"hpa": 0, "custom": 0}
    for name in order:
        kind = kinds[name]
        marks = HPA_MARKERS if kind == "hpa" else CUSTOM_MARKERS
        styles[name] = (HPA_COLOR if kind == "hpa" else CUSTOM_COLOR, marks[n[kind] % len(marks)])
        n[kind] += 1
    return styles


def fig_tradeoff(summary, order, out, fake):
    styles = setting_styles(summary, order)
    fig, axes = plt.subplots(1, 3, figsize=(7.0, 2.9), sharey=True)
    p95s = [s["p95_mean"] for s in summary if s["p95_mean"]]
    for ax, pattern in zip(axes, PATTERNS):
        style_axes(ax)
        for s in sorted((s for s in summary if s["pattern"] == pattern), key=lambda s: order.index(s["setting"])):
            if s["avg_pods_mean"] is None or s["p95_mean"] is None:
                continue
            color, marker = styles[s["setting"]]
            ax.errorbar(s["avg_pods_mean"], s["p95_mean"], xerr=s["avg_pods_sd"], yerr=s["p95_sd"],
                        fmt=marker, ms=7, color=color, mec="white", mew=0.8, elinewidth=1, capsize=2, zorder=3)
        ax.set_title(pattern.capitalize(), fontsize=9, color=INK, loc="left")
        ax.set_xlabel("Average pods", fontsize=8, color=INK)
        ax.set_xlim(0, 10.5)
    # Response times can differ 10x or more between settings, so a log scale keeps the fast ones readable.
    axes[0].set_yscale("log")
    if p95s:
        axes[0].set_ylim(min(p95s) / 1.6, max(p95s) * 1.6)
    ticks = [t for t in (10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000)
             if p95s and min(p95s) / 1.6 <= t <= max(p95s) * 1.6]
    axes[0].set_yticks(ticks, [f"{t:,}" for t in ticks])
    axes[0].minorticks_off()
    axes[0].set_ylabel("p95 response time (ms, log scale)", fontsize=8, color=INK)
    handles = [plt.Line2D([], [], marker=styles[n][1], ls="", color=styles[n][0], mec="white", ms=7, label=n)
               for n in order]
    fig.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.01, 0.91), ncol=len(order), fontsize=7.5,
               frameon=False, handletextpad=0.2, columnspacing=1.0)
    fig.suptitle("Lower-left is better: fewer pods and faster responses", x=0.01, y=0.99, ha="left",
                 fontsize=9.5, color=INK, weight="bold")
    fig.subplots_adjust(left=0.09, right=0.99, top=0.74, bottom=0.15, wspace=0.08)
    if fake:
        watermark(fig)
    fig.savefig(out / "fig2_tradeoff.png", dpi=300)
    plt.close(fig)


def fig_spike(runs, order, stages, start_rate, out, fake):
    spike = [r for r in runs if r["pattern"] == "spike"]
    length = pattern_length(stages)
    fig = plt.figure(figsize=(7.0, 3.3))
    gs = fig.add_gridspec(2, 5, height_ratios=[1, 1.8], hspace=0.55, wspace=0.1)
    top = fig.add_subplot(gs[0, :])
    style_axes(top)
    ts = [i for i in range(0, int(length) + 1)]
    top.fill_between(ts, [rate_at(stages, start_rate, t) for t in ts], color="#9c9c9c", alpha=0.35, lw=0)
    top.plot(ts, [rate_at(stages, start_rate, t) for t in ts], color=QUIET, lw=1.5)
    top.set_ylabel("Requests/s", fontsize=8, color=INK)
    top.set_xlim(0, length)
    top.set_ylim(bottom=0)
    top.set_title("Traffic sent by k6 (spike pattern)", fontsize=9, color=INK, loc="left")

    # One small panel per setting: pods ready (solid, colour = kind) and pods asked for (dashed grey).
    # The horizontal gap between the two lines after the jump is the scaling reaction time.
    names = [s for s in order if any(r["setting"] == s for r in spike)]
    ymax = max([max(r, d or 0) for run in spike for _, r, d in run["pods"]] + [1])
    grid = list(range(0, int(length) + 1, 5))

    def mean_at(mine, t, idx):  # value at time t (last sample at or before t), averaged over the repeats
        vals = []
        for r in mine:
            prior = [row[idx] for row in r["pods"] if row[0] <= t and row[idx] is not None]
            if prior:
                vals.append(prior[-1])
        return statistics.mean(vals) if vals else math.nan

    first = None
    for col, name in enumerate(names[:5]):
        ax = fig.add_subplot(gs[1, col], sharey=first)
        first = first or ax
        style_axes(ax)
        mine = [r for r in spike if r["setting"] == name]
        color = HPA_COLOR if mine[0]["kind"] == "hpa" else CUSTOM_COLOR
        ax.step(grid, [mean_at(mine, t, 2) for t in grid], where="post", color=QUIET, lw=1.2, ls="--")
        ax.step(grid, [mean_at(mine, t, 1) for t in grid], where="post", color=color, lw=2)
        ax.set_xlim(0, length)
        ax.set_ylim(0, ymax + 1)
        ax.set_xticks([0, 200, 400] if length > 400 else [0, 100, 200])
        ax.set_title(name, fontsize=8.5, color=INK, loc="left")
        if col == 0:
            ax.set_ylabel("Pods (mean of repeats)", fontsize=8, color=INK)
        else:
            plt.setp(ax.get_yticklabels(), visible=False)
        if col == 2:
            ax.set_xlabel("Seconds since the test started", fontsize=8, color=INK)
    handles = [plt.Line2D([], [], color=INK, lw=2, label="Pods ready (blue: HPA, orange: custom)"),
               plt.Line2D([], [], color=QUIET, lw=1.2, ls="--", label="Pods asked for")]
    fig.legend(handles=handles, loc="upper right", bbox_to_anchor=(0.99, 1.0), ncol=2, fontsize=7.5,
               frameon=False)
    if fake:
        watermark(fig)
    fig.subplots_adjust(left=0.08, right=0.99, top=0.89, bottom=0.12)
    fig.savefig(out / "fig3_spike_pods.png", dpi=300)
    plt.close(fig)


# ---------------------------------------------------------------- main
def main():
    here = Path(__file__).resolve()
    repo_root = here.parents[2] if len(here.parents) > 2 else Path.cwd()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", type=Path, default=repo_root / "experiments" / "results")
    ap.add_argument("--out", type=Path, default=repo_root / "docs" / "figures")
    ap.add_argument("--repeats", type=int, default=3, help="repeats expected per setting and pattern")
    args = ap.parse_args()

    stages_all, start_rate, stages_src = load_stages(repo_root)
    print(f"Traffic patterns: {stages_src}")
    folders = sorted(p for p in args.results.iterdir() if p.is_dir()) if args.results.is_dir() else []
    if not folders:
        sys.exit(f"No run folders in {args.results}")

    runs, problems = [], []
    for folder in folders:
        try:
            ident = run_identity(folder)
            k6 = read_k6(folder / "k6-summary.json")
            pods = read_pods(folder / "pods.csv")
        except Exception as e:  # report and keep going with the other runs
            problems.append(f"{folder.name}: {e}")
            continue
        stages = stages_all[ident["pattern"]]
        length = pattern_length(stages)
        during = [r for t, r, _ in pods if 0 <= t <= length]
        reaction, why = spike_reaction(pods, stages) if ident["pattern"] == "spike" else (None, "")
        runs.append({**ident, **k6, "pods": pods, "run": folder.name,
                     "avg_pods": statistics.mean(during) if during else None,
                     "max_pods": max(during) if during else None,
                     "reaction_s": reaction, "reaction_note": why})

    fake = any(r["fake"] for r in runs) or (args.results / "FAKE_DATA").exists()
    order = setting_order(runs)
    args.out.mkdir(parents=True, exist_ok=True)

    # Check coverage: every setting x pattern should have the expected repeats.
    for s in order:
        for p in PATTERNS:
            n = sum(1 for r in runs if r["setting"] == s and r["pattern"] == p)
            if n != args.repeats:
                problems.append(f"{s}, {p}: {n} runs (expected {args.repeats})")

    with (args.out / "runs.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["run", "setting", "pattern", "repeat", "p95_ms", "avg_ms", "failed_pct", "requests",
                    "avg_pods", "max_pods", "reaction_s", "reaction_note"])
        for r in sorted(runs, key=lambda r: (order.index(r["setting"]), PATTERNS.index(r["pattern"]), r["repeat"])):
            w.writerow([r["run"], r["setting"], r["pattern"], r["repeat"], f"{r['p95_ms']:.1f}", f"{r['avg_ms']:.1f}",
                        f"{r['failed_pct']:.2f}", r["requests"],
                        "" if r["avg_pods"] is None else f"{r['avg_pods']:.2f}", r["max_pods"],
                        "" if r["reaction_s"] is None else f"{r['reaction_s']:.0f}", r["reaction_note"]])

    summary = []
    for s in order:
        for p in PATTERNS:
            mine = [r for r in runs if r["setting"] == s and r["pattern"] == p]
            if not mine:
                continue
            row = {"setting": s, "kind": mine[0]["kind"], "pattern": p, "n": len(mine)}
            for key, name in (("p95_ms", "p95"), ("failed_pct", "failed"), ("avg_pods", "avg_pods"),
                              ("reaction_s", "reaction")):
                row[f"{name}_mean"], row[f"{name}_sd"] = mean_sd([r[key] for r in mine])
            summary.append(row)

    with (args.out / "summary.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["setting", "pattern", "n", "p95_ms_mean", "p95_ms_sd", "failed_pct_mean", "failed_pct_sd",
                    "avg_pods_mean", "avg_pods_sd", "reaction_s_mean", "reaction_s_sd"])
        for s in summary:
            w.writerow([s["setting"], s["pattern"], s["n"]] + [
                "" if s[k] is None else f"{s[k]:.2f}" for k in
                ("p95_mean", "p95_sd", "failed_mean", "failed_sd", "avg_pods_mean", "avg_pods_sd",
                 "reaction_mean", "reaction_sd")])

    lines = ["| Setting (spike pattern) | p95 response time (ms) | Failed requests (%) | Average pods "
             "| Reaction time (s) |", "| --- | --- | --- | --- | --- |"]
    for s in [s for s in summary if s["pattern"] == "spike"]:
        lines.append(f"| {s['setting']} | {fmt(s['p95_mean'], s['p95_sd'])} | {fmt(s['failed_mean'], s['failed_sd'], 2)} "
                     f"| {fmt(s['avg_pods_mean'], s['avg_pods_sd'], 1)} | {fmt(s['reaction_mean'], s['reaction_sd'])} |")
    note = "Mean ± standard deviation over the repeats."
    (args.out / "table2_spike.md").write_text("\n".join(lines) + "\n\n" + note + "\n")

    fig_tradeoff(summary, order, args.out, fake)
    fig_spike(runs, order, stages_all["spike"], start_rate, args.out, fake)

    print(f"Read {len(runs)} runs from {args.results}; wrote charts and tables to {args.out}")
    if fake:
        print("WARNING: input is marked as test data; the charts carry a TEST DATA watermark.")
    for p in problems:
        print("CHECK:", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
