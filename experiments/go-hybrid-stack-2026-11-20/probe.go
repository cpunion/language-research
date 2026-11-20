package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"runtime"
	"runtime/debug"
	"runtime/trace"
	"strconv"
	"strings"
	"sync"
	"time"
	"weak"
)

type state struct {
	Size   uintptr `json:"capacity_bytes"`
	Top    uintptr `json:"-"`
	Mapped bool    `json:"mapped"`
}

func snapshot() state {
	size, top, mapped := runtime.HybridStackState()
	return state{size, top, mapped}
}

type payload struct {
	value int
	pad   [128]byte
}

//go:noinline
func object(value int) *payload {
	return &payload{value: value}
}

type inspection struct {
	First, Peak, AfterFallback state
	InPlaceGrows               int
}

//go:noinline
func verifyFrame(frame *[4096]byte, value byte) {
	if frame[0] != value || frame[len(frame)-1] != ^value {
		panic("live frame corrupted")
	}
}

//go:noinline
func checkFrames(depth int, parent *[4096]byte, observed *inspection, mark chan<- weak.Pointer[payload], input <-chan *payload) {
	var frame [4096]byte
	value := byte(depth)
	frame[0], frame[len(frame)-1] = value, ^value
	current := snapshot()
	if current.Mapped {
		if observed.First.Size == 0 {
			observed.First = current
			observed.Peak = current
		} else if current.Top != observed.First.Top {
			panic("virtual stack moved during growth")
		} else if current.Size > observed.Peak.Size {
			observed.InPlaceGrows++
			observed.Peak = current
		}
	} else if observed.First.Size != 0 && observed.AfterFallback.Size == 0 {
		observed.AfterFallback = current
	}
	if depth > 0 {
		checkFrames(depth-1, &frame, observed, mark, input)
	} else {
		own := object(123)
		mark <- weak.Make(own)
		other := <-input
		if own.value != 123 || other.value != 456 {
			panic("stack GC root or channel delivery corrupted")
		}
	}
	verifyFrame(&frame, value)
	if parent != nil {
		verifyFrame(parent, byte(depth+1))
	}
}

func require(condition bool, description string) {
	if !condition {
		panic(description)
	}
}

func checkVM(expectVM bool, depth int) map[string]any {
	baseline := runtime.HybridStackMappings()
	started := make(chan state)
	mark := make(chan weak.Pointer[payload])
	input := make(chan *payload)
	returned := make(chan inspection)
	inspect := make(chan bool)
	states := make(chan state)
	done := make(chan struct{})
	go func() {
		defer close(done)
		started <- snapshot()
		var observed inspection
		checkFrames(depth, nil, &observed, mark, input)
		returned <- observed
		for regrow := range inspect {
			if regrow {
				var next inspection
				checkFrames(depth, nil, &next, mark, input)
				returned <- next
			} else {
				states <- snapshot()
			}
		}
	}()
	start := <-started
	require(!start.Mapped && start.Size < 64<<10, "shallow goroutine must start with a small ordinary stack")
	scanAndDeliver(mark, input)
	observed := <-returned
	overflow := depth > 250
	if expectVM {
		require(observed.First.Mapped && observed.First.Size == 64<<10, "missing promotion at 64 KiB")
		require(observed.InPlaceGrows >= 2, "missing in-place growth")
		if overflow {
			require(observed.AfterFallback.Size > 1<<20 && observed.AfterFallback.Top != observed.Peak.Top, "missing overflow fallback")
			require(runtime.HybridStackMappings() == baseline, "overflow left the old VM mapping active")
		} else {
			require(observed.AfterFallback.Size == 0, "unexpected fallback within VM capacity")
			require(runtime.HybridStackMappings() == baseline+1, "missing live VM mapping")
			var stats runtime.MemStats
			runtime.ReadMemStats(&stats)
			require(stats.StackSys >= stats.StackInuse+uint64(observed.Peak.Size), "VM stack missing from StackSys")
		}
	} else {
		require(observed.First.Size == 0 && runtime.HybridStackMappings() == baseline, "unexpected VM promotion")
	}
	inspect <- false
	before := <-states
	for range 15 {
		runtime.GC()
	}
	inspect <- false
	after := <-states
	require(after.Size < before.Size, "parked goroutine stack did not shrink")
	if expectVM && !overflow {
		require(after.Top == observed.Peak.Top && after.Mapped, "in-place shrinking changed stack addresses")
		require(after.Size == uintptr(os.Getpagesize()), "VM stack did not shrink to one OS page")
	}
	var regrown state
	if !overflow {
		inspect <- true
		scanAndDeliver(mark, input)
		next := <-returned
		inspect <- false
		regrown = <-states
		require(regrown.Size >= before.Size, "stack failed to grow after trim")
		if expectVM {
			require(next.First.Top == after.Top && regrown.Top == after.Top && regrown.Mapped, "stack regrowth changed stack addresses")
		}
	}
	close(inspect)
	<-done
	deadline := time.Now().Add(5 * time.Second)
	for runtime.HybridStackMappings() != baseline && time.Now().Before(deadline) {
		runtime.Gosched()
	}
	require(runtime.HybridStackMappings() == baseline, "goroutine exit failed to release its VM mapping")
	return map[string]any{
		"start": start, "first_vm": observed.First, "peak_vm": observed.Peak,
		"in_place_grows": observed.InPlaceGrows, "overflow_fallback": observed.AfterFallback,
		"before_shrink": before, "after_shrink": after,
		"same_top_after_shrink": before.Top == after.Top, "after_regrowth": regrown,
	}
}

func scanAndDeliver(mark <-chan weak.Pointer[payload], input chan<- *payload) {
	root := <-mark
	for range 4 {
		runtime.GC()
		require(root.Value() != nil, "GC lost an object held by a live stack frame")
	}
	input <- object(456)
}

//go:noinline
func recurse(depth int) uint64 {
	var frame [4096]byte
	value := byte(depth)
	frame[0], frame[len(frame)-1] = value, ^value
	var sum uint64
	if depth > 0 {
		sum = recurse(depth - 1)
	}
	verifyFrame(&frame, value)
	return sum + uint64(frame[0]) + uint64(frame[len(frame)-1])
}

func workload(name string, count int) uint64 {
	var sum uint64
	switch name {
	case "shallow":
		var ready, done sync.WaitGroup
		ready.Add(count)
		done.Add(count)
		release := make(chan struct{})
		for range count {
			go func() {
				ready.Done()
				<-release
				done.Done()
			}()
		}
		ready.Wait()
		close(release)
		done.Wait()
		sum = uint64(count)
	case "deep":
		finished := make(chan uint64)
		go func() {
			finished <- recurse(count)
		}()
		sum = <-finished
	case "churn":
		const parallel = 32
		finished := make(chan uint64, parallel)
		for first := 0; first < count; first += parallel {
			batch := min(parallel, count-first)
			for range batch {
				go func() {
					finished <- recurse(80)
				}()
			}
			for range batch {
				sum += <-finished
			}
		}
	case "oscillate":
		start := make(chan struct{})
		finished := make(chan uint64)
		go func() {
			for range start {
				finished <- recurse(160)
			}
		}()
		for range count {
			start <- struct{}{}
			sum += <-finished
			for range 4 {
				runtime.GC()
			}
		}
		close(start)
	default:
		panic("unknown workload")
	}
	return sum
}

func linuxPeakRSSKiB() uint64 {
	status, err := os.ReadFile("/proc/self/status")
	if err != nil {
		panic(err)
	}
	for _, line := range strings.Split(string(status), "\n") {
		if value, ok := strings.CutPrefix(line, "VmHWM:"); ok {
			fields := strings.Fields(value)
			require(len(fields) == 2 && fields[1] == "kB", "unexpected VmHWM format")
			peak, err := strconv.ParseUint(fields[0], 10, 64)
			if err != nil {
				panic(err)
			}
			return peak
		}
	}
	panic("missing VmHWM in /proc/self/status")
}

func main() {
	check := flag.String("check", "", "run correctness checks for vm or copy mode")
	traceCheck := flag.Bool("trace", false, "enable execution tracing during the correctness checks")
	bench := flag.String("bench", "", "run the shallow, deep, churn, or oscillate workload")
	count := flag.Int("count", 0, "goroutine count or, for deep, recursion depth")
	flag.Parse()
	if *check == "vm" || *check == "copy" {
		if *traceCheck {
			if err := trace.Start(io.Discard); err != nil {
				panic(err)
			}
			defer trace.Stop()
		}
		json.NewEncoder(os.Stdout).Encode(map[string]any{
			"go_version":      runtime.Version(),
			"within_capacity": checkVM(*check == "vm", 100),
			"above_capacity":  checkVM(*check == "vm", 350),
			"live_mappings":   runtime.HybridStackMappings(),
		})
		return
	}
	if *count < 1 || *bench == "" {
		flag.Usage()
		os.Exit(2)
	}
	// Keep automatic GC from changing when the workload measures growth.
	// The oscillating workload invokes a fixed number of collections.
	debug.SetGCPercent(-1)
	start := time.Now()
	sum := workload(*bench, *count)
	elapsed := time.Since(start)
	expected := uint64(*count)
	switch *bench {
	case "deep":
		expected = uint64(*count+1) * 255
	case "churn":
		expected *= 81 * 255
	case "oscillate":
		expected *= 161 * 255
	}
	require(sum == expected, fmt.Sprintf("got checksum %d, want %d", sum, expected))
	result := map[string]any{"ns": elapsed.Nanoseconds(), "checksum": sum}
	if runtime.GOOS == "linux" {
		// The process rusage can retain the RSS high-water mark of the image
		// before exec. VmHWM reports the peak of this address space.
		result["peak_rss_kib"] = linuxPeakRSSKiB()
	}
	json.NewEncoder(os.Stdout).Encode(result)
}
