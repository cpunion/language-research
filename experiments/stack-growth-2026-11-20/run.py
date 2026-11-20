#!/usr/bin/env python3
"""Build and compare stack storage backends in fresh processes."""

import argparse
import hashlib
import itertools
import json
import os
from pathlib import Path
import platform
import random
import statistics
import subprocess
import tempfile


MODES = ("copy", "vm-protect", "vm-lazy")
WORKLOADS = ("shallow", "deep", "cycle-keep", "cycle-trim")
FLAGS = ("-std=c11", "-O3", "-Wall", "-Wextra", "-Werror")
SOURCE = Path(__file__).with_name("bench.c").resolve()


def verify(samples):
    for workload in WORKLOADS:
        rows = [r for r in samples if r["workload"] == workload]
        assert len({r["checksum"] for r in rows}) == 1, workload
        for row in rows:
            count, depth, waves = (row[k] for k in ("stacks", "depth", "waves"))
            seeds = 1024 * count * (count - 1) // 2
            full = depth * seeds + count * depth * (depth + 1) // 2
            rewind = 2 * seeds + 3 * count if waves > 1 else 0
            assert row["checksum"] == waves * (full + rewind), row
            assert len(row["wave_ns"]) == waves
            copy = row["mode"] == "copy"
            initial = 2048 if copy else row["page_bytes"]
            old_capacities = []
            while initial < depth * 512:
                old_capacities.append(initial)
                initial *= 2
            trim = workload == "cycle-trim"
            growth_stacks = count * (waves if trim else 1)
            assert row["grows"] == len(old_capacities) * growth_stacks, row
            assert row["trims"] == (count * waves if trim else 0), row
            expected_copies = sum(old_capacities) * growth_stacks + 1024 * row["trims"]
            expected_links = sum(n // 512 - 1 for n in old_capacities) * growth_stacks + row["trims"]
            assert row["copied_bytes"] == (expected_copies if copy else 0), row
            assert row["repaired_links"] == (expected_links if copy else 0), row
            assert row["trim_os_calls"] == (0 if copy else row["trims"]), row
            protects = count + row["grows"] if row["mode"] == "vm-protect" else 0
            assert row["mprotect_calls"] == protects, row


def summarize(samples, cases):
    summary = []
    for workload, mode in cases:
        rows = [r for r in samples if (r["workload"], r["mode"]) == (workload, mode)]
        total = [sum(r[k] for k in ("setup_ns", "fill_and_check_ns", "rewind_and_check_ns", "teardown_ns"))
                 for r in rows]
        median = lambda key: statistics.median(r[key] for r in rows)
        summary.append(dict(workload=workload, mode=mode,
                            total_median_ms=statistics.median(total) / 1e6,
                            total_range_ms=[min(total) / 1e6, max(total) / 1e6],
                            setup_median_ms=median("setup_ns") / 1e6,
                            sampled_full_rss_mib=median("full_rss") / 2**20,
                            final_rss_mib=median("after_rewind_rss") / 2**20,
                            work_faults=median("work_faults"),
                            copied_mib=median("copied_bytes") / 2**20,
                            repaired_links=median("repaired_links")))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cc", default=os.environ.get("CC", "cc"))
    parser.add_argument("--runs", type=int, default=9)
    parser.add_argument("--label", default="local")
    parser.add_argument("--linux-nohuge", action="store_true",
                        help="also measure Linux shallow lazy stacks with MADV_NOHUGEPAGE")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    cases = list(itertools.product(WORKLOADS, MODES))
    if args.linux_nohuge:
        if platform.system() != "Linux":
            parser.error("--linux-nohuge requires Linux")
        cases.append(("shallow", "vm-lazy-nohuge"))
    samples = []
    with tempfile.TemporaryDirectory(prefix="stack-growth-") as directory:
        binary = Path(directory) / "bench"
        subprocess.run([args.cc, *FLAGS, str(SOURCE), "-o", str(binary)], check=True)
        rng = random.Random(20261120)
        order = cases[:]
        for iteration in range(args.runs):
            rng.shuffle(order)
            for workload, mode in order:
                output = subprocess.check_output([binary, mode, workload], text=True, timeout=30)
                samples.append(dict(iteration=iteration, workload=workload, mode=mode,
                                    **json.loads(output)))
    verify(samples)
    result = dict(label=args.label, system=platform.system(), release=platform.release(),
                  machine=platform.machine(), page_bytes=samples[0]["page_bytes"],
                  compiler=subprocess.check_output([args.cc, "--version"], text=True).splitlines()[0],
                  flags=FLAGS, source_sha256=hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
                  repetitions=args.runs, summary=summarize(samples, cases), samples=samples)
    if platform.system() == "Linux":
        thp = Path("/sys/kernel/mm/transparent_hugepage")
        settings = [thp / "enabled", *sorted(thp.glob("hugepages-*/enabled"))]
        result["transparent_hugepage_settings"] = {
            str(path.relative_to(thp)): path.read_text().strip() for path in settings if path.is_file()
        }
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
        print(f"Verified {len(samples)} samples. Wrote {args.output}.")
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
