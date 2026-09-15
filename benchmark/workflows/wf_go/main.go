// wf_go -- the Go side of the workflow comparison, plus the shared echo server.
//
// Same workloads and parameters as ../common.py, written the canonical Go way:
// goroutine per concurrent unit, buffered channels for the pool/pipeline,
// sync.WaitGroup to join, time.Sleep for timers, blocking net.Conn I/O.
//
//	go run . -workload fanout_io [-n N] [-addr host:port] [-procs P]
//	go run . -echoserver -addr host:port      (prints LISTENING, serves forever)
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"runtime"
	"strconv"
	"sync"
	"syscall"
	"time"
)

// peakRSS mirrors common.peak_rss_bytes: ru_maxrss is bytes on Darwin and
// kilobytes on Linux/BSD.
func peakRSS() int64 {
	var ru syscall.Rusage
	if err := syscall.Getrusage(syscall.RUSAGE_SELF, &ru); err != nil {
		return -1
	}
	if runtime.GOOS == "darwin" {
		return int64(ru.Maxrss)
	}
	return int64(ru.Maxrss) * 1024
}

// Mirrors of common.py constants -- keep in sync.
const (
	fanoutK     = 50
	producers   = 8
	workers     = 32
	queueCap    = 256
	pipelineCap = 128
	cpuIter     = 400000
	sleepDur    = 5 * time.Millisecond
	sleepK      = 20
	churnIter   = 50
	mixedK      = 20
	mixedDB     = 1 * time.Millisecond
)

var req = []byte("hello-workflow-\n")

func envInt(name string, def int) int {
	if v := os.Getenv(name); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

var (
	mixedCPU = envInt("WF_MIXED_CPU", 200) // mirrors common.MIXED_CPU
	ports    = envInt("WF_PORTS", 1)       // mirrors common.PORTS
	jobIter  = envInt("WF_JOB_ITER", 200)  // mirrors common.JOB_ITER
)

// addrFor spreads client i over the echo server's `ports` consecutive ports.
func addrFor(addr string, i int) string {
	host, portStr, err := net.SplitHostPort(addr)
	if err != nil {
		return addr
	}
	port, _ := strconv.Atoi(portStr)
	return net.JoinHostPort(host, strconv.Itoa(port+i%ports))
}

var defaultN = map[string]int{
	"fanout_io": 500, "worker_pool": 20000, "pipeline": 50000,
	"cpu_parallel": 64, "sleepers": 5000, "mixed": 200, "spawn_churn": 100000,
}

// Same 32-bit LCG recurrence as common.py (see the note there on why the
// CPU kernel is arithmetic rather than sha256).
func lcg(x uint32, n int) uint32 {
	for i := 0; i < n; i++ {
		x = x*1103515245 + 12345
	}
	return x
}

func hashJob() uint32 { return lcg(1, jobIter) }

func cpuChain() uint32 { return lcg(1, cpuIter) }

type pipeItem struct {
	Seq   int    `json:"seq"`
	Stage int    `json:"stage"`
	Body  string `json:"body"`
}

func pipelineSeed(i int) []byte {
	b, _ := json.Marshal(pipeItem{Seq: i, Stage: 0, Body: string(make64b())})
	return b
}

func make64b() []byte {
	b := make([]byte, 64)
	for i := range b {
		b[i] = 'b'
	}
	return b
}

func pipelineStage(i int, item []byte) []byte {
	var d pipeItem
	_ = json.Unmarshal(item, &d)
	d.Stage = i
	d.Seq++
	b, _ := json.Marshal(d)
	return b
}

type mixedDoc struct {
	ID    int      `json:"id"`
	User  string   `json:"user"`
	Items []int    `json:"items"`
	Tags  []string `json:"tags"`
}

var mixedDocBytes = func() []byte {
	b, _ := json.Marshal(mixedDoc{ID: 1, User: "uuuuuuuuuuuuuuuu",
		Items: []int{0, 1, 2, 3, 4, 5, 6, 7, 8, 9}, Tags: []string{"a", "b", "c"}})
	return b
}()

func mixedTransform(doc []byte) []byte {
	var d mixedDoc
	_ = json.Unmarshal(doc, &d)
	for i := range d.Items {
		d.Items[i] *= 2
	}
	lcg(uint32(d.ID), mixedCPU) // the request's "business logic"
	b, _ := json.Marshal(d)
	return b
}

func dial(addr string) net.Conn {
	c, err := net.Dial("tcp", addr)
	if err != nil {
		fmt.Fprintln(os.Stderr, "dial:", err)
		os.Exit(1)
	}
	if tc, ok := c.(*net.TCPConn); ok {
		_ = tc.SetNoDelay(true)
		// RST on close (see common.rst_close): no TIME_WAIT holding one of
		// macOS's ~16k per-IP ephemeral ports for 30 s.
		_ = tc.SetLinger(0)
	}
	return c
}

func roundTrip(c net.Conn, buf []byte) {
	if _, err := c.Write(req); err != nil {
		panic(err)
	}
	if _, err := io.ReadFull(c, buf); err != nil {
		panic(err)
	}
}

func spawnAll(n int, fn func()) {
	spawnAllIdx(n, func(int) { fn() })
}

func spawnAllIdx(n int, fn func(i int)) {
	var wg sync.WaitGroup
	wg.Add(n)
	for i := 0; i < n; i++ {
		go func(i int) { defer wg.Done(); fn(i) }(i)
	}
	wg.Wait()
}

// startMark is set once a workload's setup phase (connect ramp) is done;
// the reported seconds count from there.  Mirrors common.mark_start().
var startMark time.Time

// gated runs fn on n goroutines; each calls ready() after its setup and
// blocks until all n have, then the timed window opens (WaitGroup +
// closed channel -- the canonical Go barrier).
func gated(n int, fn func(i int, ready func())) {
	var readyWG sync.WaitGroup
	readyWG.Add(n)
	start := make(chan struct{})
	ready := func() { readyWG.Done(); <-start }
	go func() {
		readyWG.Wait()
		startMark = time.Now()
		close(start)
	}()
	spawnAllIdx(n, func(i int) { fn(i, ready) })
}

func fanoutIO(n int, addr string) int {
	gated(n, func(ci int, ready func()) {
		c := dial(addrFor(addr, ci))
		defer c.Close()
		ready()
		buf := make([]byte, len(req))
		for i := 0; i < fanoutK; i++ {
			roundTrip(c, buf)
		}
	})
	return n * fanoutK
}

func workerPool(n int) int {
	per := n / producers
	total := per * producers
	jobs := make(chan int, queueCap)
	results := make(chan uint32, queueCap)
	var wgW sync.WaitGroup
	wgW.Add(workers)
	for i := 0; i < workers; i++ {
		go func() {
			defer wgW.Done()
			for range jobs {
				results <- hashJob()
			}
		}()
	}
	done := make(chan struct{})
	go func() {
		for i := 0; i < total; i++ {
			<-results
		}
		close(done)
	}()
	spawnAll(producers, func() {
		for i := 0; i < per; i++ {
			jobs <- i
		}
	})
	close(jobs)
	wgW.Wait()
	<-done
	return total
}

func pipeline(n int) int {
	const stages = 4
	chans := make([]chan []byte, stages+1)
	for i := range chans {
		chans[i] = make(chan []byte, pipelineCap)
	}
	go func() {
		for i := 0; i < n; i++ {
			chans[0] <- pipelineSeed(i)
		}
		close(chans[0])
	}()
	for i := 0; i < stages; i++ {
		go func(i int) {
			for item := range chans[i] {
				chans[i+1] <- pipelineStage(i+1, item)
			}
			close(chans[i+1])
		}(i)
	}
	cnt := 0
	for range chans[stages] {
		cnt++
	}
	return cnt
}

func cpuParallel(n int) int {
	spawnAll(n, func() { cpuChain() })
	return n * cpuIter
}

func spawnChurn(n int) int {
	spawnAll(n, func() { lcg(1, churnIter) })
	return n
}

func sleepers(n int) int {
	spawnAll(n, func() {
		for i := 0; i < sleepK; i++ {
			time.Sleep(sleepDur)
		}
	})
	return n * sleepK
}

func mixed(n int, addr string) int {
	gated(n, func(ci int, ready func()) {
		c := dial(addrFor(addr, ci))
		defer c.Close()
		ready()
		buf := make([]byte, len(req))
		for i := 0; i < mixedK; i++ {
			roundTrip(c, buf)
			mixedTransform(mixedDocBytes)
			time.Sleep(mixedDB)
			roundTrip(c, buf)
		}
	})
	return n * mixedK
}

// echoServer: goroutine-per-connection echo, the shared backend every
// runtime's fanout_io / mixed clients talk to.  Go on purpose: it is the
// same fixed target for all four, so it is never the thing being measured.
func echoServer(addr string) {
	var lns []net.Listener
	for i := 0; i < ports; i++ {
		ln, err := net.Listen("tcp", addrFor(addr, i))
		if err != nil {
			fmt.Fprintln(os.Stderr, "listen:", err)
			os.Exit(1)
		}
		lns = append(lns, ln)
	}
	fmt.Println("LISTENING", lns[0].Addr().String(), "ports", ports)
	for _, ln := range lns[1:] {
		go acceptLoop(ln)
	}
	acceptLoop(lns[0])
}

func acceptLoop(ln net.Listener) {
	for {
		c, err := ln.Accept()
		if err != nil {
			continue
		}
		go func(c net.Conn) {
			defer c.Close()
			if tc, ok := c.(*net.TCPConn); ok {
				_ = tc.SetNoDelay(true)
			}
			buf := make([]byte, 4096)
			for {
				n, err := c.Read(buf)
				if err != nil {
					return
				}
				if _, err := c.Write(buf[:n]); err != nil {
					return
				}
			}
		}(c)
	}
}

func main() {
	workload := flag.String("workload", "", "one of fanout_io worker_pool pipeline cpu_parallel sleepers mixed")
	n := flag.Int("n", 0, "workload size (0 = default)")
	addr := flag.String("addr", "127.0.0.1:19876", "echo server address")
	procs := flag.Int("procs", 0, "GOMAXPROCS (0 = runtime default)")
	echo := flag.Bool("echoserver", false, "run the echo server instead of a workload")
	flag.Parse()
	if *echo {
		echoServer(*addr)
		return
	}
	if *procs > 0 {
		runtime.GOMAXPROCS(*procs)
	}
	if *n <= 0 {
		*n = defaultN[*workload]
	}
	rssStart := peakRSS()
	var ops int
	t0 := time.Now()
	switch *workload {
	case "fanout_io":
		ops = fanoutIO(*n, *addr)
	case "worker_pool":
		ops = workerPool(*n)
	case "pipeline":
		ops = pipeline(*n)
	case "cpu_parallel":
		ops = cpuParallel(*n)
	case "sleepers":
		ops = sleepers(*n)
	case "mixed":
		ops = mixed(*n, *addr)
	case "spawn_churn":
		ops = spawnChurn(*n)
	default:
		fmt.Fprintln(os.Stderr, "unknown workload", *workload)
		os.Exit(2)
	}
	if !startMark.IsZero() {
		t0 = startMark
	}
	secs := time.Since(t0).Seconds()
	rssPeak := peakRSS()
	out, _ := json.Marshal(map[string]any{
		"runtime": "go", "workload": *workload, "n": *n, "ops": ops,
		"seconds": secs, "ops_per_s": float64(ops) / secs,
		"workers": runtime.GOMAXPROCS(0), "go": runtime.Version(),
		"rss_start_bytes": rssStart, "rss_peak_bytes": rssPeak,
		"rss_delta_bytes": rssPeak - rssStart,
		"bytes_per_unit":  float64(rssPeak-rssStart) / float64(*n),
	})
	fmt.Println(string(out))
}
