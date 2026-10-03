// Package support gathers what support needs to debug a problem on the
// MSME's computer without asking them: a system report (Windows, memory,
// Tally's path and file version, add-ons from tally.ini, whether Windows sees
// Tally as not responding) and Tally's CPU and memory while a request is
// stuck. The report is sent through Send, which the connector points at the
// server while a sync or check runs.
package support

import (
	"fmt"
	"log"
	"sync"
	"time"
)

// Send uploads one item ("system") for support; nil when
// nothing is connected.
var Send func(kind, text string)

var (
	mu      sync.Mutex
	lastCPU float64
)

// Report describes this computer and its Tally.
func Report() string {
	return fmt.Sprintf("Connector system report, %s\n%s", time.Now().Format(time.RFC1123), gather())
}

// Reset starts a new sync.
func Reset() {
	mu.Lock()
	lastCPU = 0
	mu.Unlock()
}

// Status is a one-line description of Tally's process right now.
func Status() string {
	_, line := sample()
	return line
}

// OnSlow is called while a Tally request has been running for a while: it
// logs Tally's CPU and memory, which tells a busy Tally from a waiting one.
func OnSlow(what string, waited time.Duration) {
	cpu, line := sample()
	mu.Lock()
	delta := cpu - lastCPU
	if lastCPU == 0 {
		delta = 0
	}
	lastCPU = cpu
	mu.Unlock()
	if line != "" {
		log.Printf("tally: still waiting on %s after %s; %s (CPU +%.1f s since last check)", what, waited.Round(time.Second), line, delta)
	}
}
