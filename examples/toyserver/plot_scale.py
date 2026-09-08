"""Draw the scaling chart for the measurement experiments.

Reads every finished measurement run under ./runs/ (the test_measure.py
experiments — runs from your fault tests are skipped) and turns the
numbers into one picture: how many requests per second the cluster
completed, at each fleet size you tried, for each kind of workload.
Latency (the p99 — how long the slowest 1-in-100 requests took) is
printed to the terminal instead of drawn, so the chart stays uncluttered.

Usage:  uv run python plot_scale.py      (after running test_measure.py)
"""

import json
import sys
from pathlib import Path

# matplotlib is the one thing this script needs that the project doesn't
# already have, so check for it before doing anything else.
try:
    import matplotlib
    matplotlib.use("Agg")        # draw straight to a file — no window needed
    import matplotlib.pyplot as plt
except ImportError:
    sys.exit("one-time setup: run `uv add matplotlib`, then re-run this")


def workload_kind(name):
    """A run is named after the test that produced it, and the test's name
    tells us what mix of requests it sent. Runs from other tests (the
    kill and partition tests, say) return None and stay off the chart —
    their "throughput" measures a fault schedule, not capacity."""
    if "read" in name:
        return "reads only"
    if "mixed" in name:
        return "reads + writes"
    return None


# Collect one number per (workload, fleet size). Run folders start with a
# timestamp, so sorting them by name puts the newest last — and since later
# runs overwrite earlier ones in `picked`, the newest run wins.
runs_dir = Path("runs")
run_dirs = sorted(p for p in runs_dir.iterdir() if p.is_dir()) if runs_dir.is_dir() else []
picked = {}                      # (kind, nodes) -> {"ops", "p99", "older"}
skipped = 0
not_measurements = 0
for run_dir in run_dirs:
    try:
        meta = json.loads((run_dir / "metadata.json").read_text())
        report = json.loads((run_dir / "report.json").read_text())
    except (OSError, json.JSONDecodeError):
        skipped += 1             # a crashed or half-finished run; move on
        continue
    kind = workload_kind(meta["name"])
    if kind is None:
        not_measurements += 1    # a fault test's run; not for this chart
        continue
    key = (kind, len(meta["nodes"]))
    older = picked[key]["older"] + 1 if key in picked else 0
    picked[key] = {"ops": report["throughput_ops_per_s"],
                   "p99": report["ok_latency_ms"].get("p99", "?"), "older": older}

if not picked:
    sys.exit("run the scale experiments first — see the introductory lab, Act 3")
if skipped:
    print(f"(ignored {skipped} unfinished run folder{'s' * (skipped != 1)})")
if not_measurements:
    print(f"({not_measurements} run{'s' * (not_measurements != 1)} from "
          "other tests left off the chart — it only plots the "
          "measurement experiments)")
for (kind, nodes), r in sorted(picked.items()):
    if r["older"]:
        print(f"using your most recent {kind} run at {nodes} nodes; "
              f"{r['older']} older one{'s' * (r['older'] != 1)} ignored")
    print(f"{kind} at {nodes} nodes: p99 latency {r['p99']} ms")

# The chart: node counts along the bottom, one bar per workload at each.
node_counts = sorted({nodes for _, nodes in picked})
# "reads only" comes first (it's the experiment you run first), then the rest.
kinds = sorted({kind for kind, _ in picked}, key=lambda k: (k != "reads only", k))
colors = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]   # fixed series colors

fig, ax = plt.subplots(figsize=(7, 4.5), dpi=150)
width = 0.8 / len(kinds)         # split each slot evenly between workloads
for i, kind in enumerate(kinds):
    have = [n for n in node_counts if (kind, n) in picked]
    xs = [node_counts.index(n) + (i - (len(kinds) - 1) / 2) * width for n in have]
    bars = ax.bar(xs, [picked[(kind, n)]["ops"] for n in have],
                  width=width * 0.94, color=colors[i % len(colors)], label=kind)
    ax.bar_label(bars, fmt="%.0f", fontsize=9, color="#3d3d3a")

ax.set_xticks(range(len(node_counts)), [str(n) for n in node_counts])
ax.set_xlabel("number of server nodes")
ax.set_ylabel("requests completed per second")
ax.set_title("Does adding nodes make the service faster?")
if len(kinds) > 1:
    ax.legend(frameon=False)
ax.spines[["top", "right"]].set_visible(False)
ax.grid(axis="y", color="#e5e4df", linewidth=0.8)
ax.set_axisbelow(True)           # grid lines behind the bars, not over them

fig.tight_layout()
fig.savefig("scale.png")
print("wrote scale.png — open it to see how throughput scaled")
