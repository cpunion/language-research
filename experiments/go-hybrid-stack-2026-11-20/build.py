#!/usr/bin/env python3
"""Build the pinned Go runtime experiment using a disposable source overlay."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


HERE = Path(__file__).resolve().parent
VERSION = "go1.27.3"


def replace(source, before, after):
    if source.count(before) != 1:
        raise ValueError(f"expected one occurrence of {before!r} in runtime source")
    return source.replace(before, after)


def overlay(runtime, directory):
    edits = {
        "mstats.go": [
            ("// only counts newosproc0 stack in mstats; differs from MemStats.StackSys",
             "// goroutine stacks backed by VM and OS thread stacks allocated by the runtime"),
            ("// In non-cgo programs this metric is currently equal to StackInuse",
             "// With hybrid stacks enabled, this also includes their usable capacity.\n"
             "\t// Otherwise, in non-cgo programs this metric is currently equal to StackInuse"),
        ],
        "runtime1.go": [
            ("\tharddecommit             int32\n", "\tharddecommit             int32\n\thybridstack              int32\n"),
            ('\t{name: "inittrace",', '\t{name: "hybridstack", value: &debug.hybridstack},\n\t{name: "inittrace",'),
        ],
        "runtime2.go": [
            ("\tvalgrindStackID uintptr\n}", "\tvalgrindStackID uintptr\n\n\tstackVM bool // the current stack uses the experimental VM allocator\n}"),
        ],
        "stack.go": [
            ("// Copies gp's stack to a new stack of a different size.\n// Caller must have changed gp status to Gcopystack.\n",
             "// Resizes gp's stack, copying it if necessary.\n// The caller must own gp's stack.\n"),
            ("\tused := old.hi - gp.sched.sp\n\t// Add just the difference",
             """\toldSize := old.hi - old.lo
	oldVM := gp.stackVM
	if oldVM && newsize != oldSize && newsize <= vmStackCapacity {
		if resizeVMStack(gp, newsize) {
			gcController.addScannableStack(getg().m.p.ptr(), int64(newsize)-int64(oldSize))
		}
		return
	}
	used := old.hi - gp.sched.sp
	// Add just the difference"""),
            ("\tnew := stackalloc(uint32(newsize))\n",
             """\tvar new stack
	if debug.hybridstack != 0 && !oldVM && newsize > oldSize {
		new = allocVMStack(newsize)
	}
	newVM := new.lo != 0
	if !newVM {
		new = stackalloc(uint32(newsize))
	}
"""),
            ("\tgp.stack = new\n", "\tgp.stack = new\n\tgp.stackVM = newVM\n"),
            ("\tstackfree(old)\n}",
             "\tif oldVM {\n\t\tfreeVMStack(old)\n\t} else {\n\t\tstackfree(old)\n\t}\n}"),
            ("\tif newsize < fixedStack {\n", "\tif newsize < fixedStack || (gp.stackVM && newsize < physPageSize) {\n"),
        ],
        "proc.go": [
            ("\tif stksize != uintptr(startingStackSize) {\n\t\t// non-standard stack size - free it.\n\t\tstackfree(gp.stack)\n",
             """\tif gp.stackVM || stksize != uintptr(startingStackSize) {
		// Only ordinary starting-size stacks can enter the free G cache.
		if gp.stackVM {
			freeVMStack(gp.stack)
			gp.stackVM = false
		} else {
			stackfree(gp.stack)
		}
"""),
        ],
    }
    files = {}
    for name, changes in edits.items():
        source = (runtime / name).read_text()
        for before, after in changes:
            source = replace(source, before, after)
        output = directory / name
        output.write_text(source)
        files[str(runtime / name)] = str(output)

    for template in sorted(HERE.glob("vmstack*.go.in")):
        output = directory / template.stem
        output.write_text(template.read_text())
        files[str(runtime / output.name)] = str(output)

    probe = directory / "vmstack_probe.go"
    probe.write_text("""package runtime

// HybridStackState observes the calling goroutine's capacity and stack top.
//go:nosplit
func HybridStackState() (size, top uintptr, mapped bool) {
	gp := getg()
	return gp.stack.hi - gp.stack.lo, gp.stack.hi, gp.stackVM
}
""")
    files[str(runtime / probe.name)] = str(probe)
    manifest = directory / "overlay.json"
    manifest.write_text(json.dumps({"Replace": files}))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output", required=True, type=Path)
    parser.add_argument("--go", default="go")
    parser.add_argument("--goos")
    parser.add_argument("--goarch")
    parser.add_argument("--runtime-tests", action="store_true")
    args = parser.parse_args()

    env = dict(os.environ, GOTOOLCHAIN="local")
    if args.goos:
        env["GOOS"] = args.goos
    if args.goarch:
        env["GOARCH"] = args.goarch
    env["CGO_ENABLED"] = "0"
    info = json.loads(subprocess.check_output([args.go, "env", "-json", "GOROOT", "GOVERSION"], env=env))
    if info["GOVERSION"] != VERSION:
        parser.error(f"this overlay requires {VERSION}; found {info['GOVERSION']}")
    with tempfile.TemporaryDirectory(prefix="go-vm-stack-") as tmp:
        manifest = overlay(Path(info["GOROOT"]) / "src" / "runtime", Path(tmp))
        flags = [f"-overlay={manifest}", "-o", str(args.output.resolve())]
        if args.runtime_tests:
            command = [args.go, "test", "-c", *flags, "runtime"]
        else:
            command = [args.go, "build", *flags, str(HERE / "probe.go")]
        subprocess.run(command, env=env, check=True)


if __name__ == "__main__":
    main()
