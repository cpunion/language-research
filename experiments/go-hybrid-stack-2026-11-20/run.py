#!/usr/bin/env python3
"""Validate and measure the hybrid stack probe on the local operating system."""

import argparse
import json
import os
from pathlib import Path
import platform
from statistics import median
import subprocess
import tempfile


WORKLOADS = {"shallow": 16_384, "deep": 200, "churn": 512, "oscillate": 8}


def invoke(binary, mode, args, checkmark=False):
    godebug = f"hybridstack={int(mode == 'hybrid')},adaptivestackstart=0"
    if checkmark:
        godebug += ",gccheckmark=1"
    env = dict(os.environ, GODEBUG=godebug, GOMAXPROCS="4", GOGC="100", GOMEMLIMIT="off")
    with tempfile.TemporaryFile() as output:
        with subprocess.Popen([binary, *args], env=env, stdout=output) as child:
            _, status, usage = os.wait4(child.pid, 0)
            child.returncode = os.waitstatus_to_exitcode(status)
            if child.returncode:
                raise subprocess.CalledProcessError(child.returncode, child.args)
        output.seek(0)
        result = json.load(output)
    rss_kib = usage.ru_maxrss // 1024 if platform.system() == "Darwin" else result.get("peak_rss_kib")
    return result, rss_kib


def benchmark(binary, name, count, repetitions):
    args = ["-bench", name, "-count", str(count)]
    modes = ("copy", "hybrid")
    for mode in modes:
        for _ in range(3):
            invoke(binary, mode, args)
    samples = {mode: [] for mode in modes}
    checksums = set()
    for pair in range(repetitions):
        for mode in modes if pair % 2 == 0 else reversed(modes):
            result, rss = invoke(binary, mode, args)
            samples[mode].append([result["ns"], rss])
            checksums.add(result["checksum"])
    if len(checksums) != 1:
        raise ValueError(f"workload {name} produced differing checksums")
    return {
        "count": count,
        "checksum": checksums.pop(),
        "modes": {
            mode: {
                "median_ns": median(row[0] for row in rows),
                "median_peak_rss_kib": median(row[1] for row in rows),
                "samples_ns_rss_kib": rows,
            }
            for mode, rows in samples.items()
        },
        "median_paired_time_ratio": round(median(
            hybrid[0] / copy[0] for copy, hybrid in zip(samples["copy"], samples["hybrid"])
        ), 5),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("-o", "--output", required=True, type=Path)
    parser.add_argument("--pairs", type=int, default=31)
    parser.add_argument("--environment", default="")
    args = parser.parse_args()
    if args.pairs < 1:
        parser.error("--pairs must be positive")
    binary = str(args.binary.resolve())
    checks = {}
    for mode, check in [("copy", "copy"), ("hybrid", "vm")]:
        checks[mode] = invoke(binary, mode, ["-check", check])[0]
        invoke(binary, mode, ["-check", check, "-trace"], checkmark=True)
    result = {
        "system": platform.system(),
        "release": platform.release(),
        "architecture": platform.machine(),
        "environment": args.environment,
        "page_bytes": os.sysconf("SC_PAGE_SIZE"),
        "rss_source": "wait4 ru_maxrss" if platform.system() == "Darwin" else "/proc/self/status VmHWM",
        "go_version": checks["hybrid"]["go_version"],
        "gomaxprocs": 4,
        "pairs_per_workload": args.pairs,
        "correctness": checks,
        "benchmarks": {},
    }
    for name, count in WORKLOADS.items():
        measured = benchmark(binary, name, count, args.pairs)
        result["benchmarks"][name] = measured
        print(f"{name} hybrid/copy median paired time ratio {measured['median_paired_time_ratio']}", flush=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
