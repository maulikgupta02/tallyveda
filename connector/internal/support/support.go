// Package support gathers what support needs to debug a problem on the
// MSME's computer without asking them: a system report (Windows, Tally
// release, add-ons from tally.ini, whether Windows sees Tally as not
// responding), Tally's CPU and memory while a request is stuck, and one
// picture of the Tally window. Everything is sent through Send, which the
// connector points at the server while a sync or check runs.
package support

import (
	"encoding/base64"
	"fmt"
	"log"
	"sync"
	"time"
)

// Send uploads one item ("system" or "screenshot") for support; nil when
// nothing is connected.
var Send func(kind, text string)

var (
	mu        sync.Mutex
	shotTaken bool
	lastCPU   float64
)

// Report describes this computer and its Tally.
func Report() string {
	return fmt.Sprintf("Connector system report, %s\n%s", time.Now().Format(time.RFC1123), gather())
}

// Reset starts a new sync: one picture per sync at most.
func Reset() {
	mu.Lock()
	shotTaken, lastCPU = false, 0
	mu.Unlock()
}

// Status is a one-line description of Tally's process right now.
func Status() string {
	_, line := sample()
	return line
}

// OnSlow is called while a Tally request has been running for a while.
func OnSlow(what string, waited time.Duration) {
	cpu, line := sample()
	mu.Lock()
	delta := cpu - lastCPU
	if lastCPU == 0 {
		delta = 0
	}
	lastCPU = cpu
	takeShot := !shotTaken && waited >= 30*time.Second
	if takeShot {
		shotTaken = true
	}
	mu.Unlock()
	if line != "" {
		log.Printf("tally: still waiting on %s after %s; %s (CPU +%.1f s since last check)", what, waited.Round(time.Second), line, delta)
	}
	if takeShot && Send != nil {
		if img := screenshot(); len(img) > 0 {
			Send("screenshot", base64.StdEncoding.EncodeToString(img))
			log.Printf("sent a picture of the Tally window to support (%d KB)", len(img)/1024)
		}
	}
}
