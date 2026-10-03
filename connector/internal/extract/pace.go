package extract

import (
	"context"
	"errors"
	"log"
	"time"

	"tallyconnector/internal/activity"
	"tallyconnector/internal/tally"
)

// Tally answers XML requests on the same thread that draws its screen, so a
// long request freezes Tally for whoever is using it. Every request therefore
// gets a time limit and is followed by a pause, and a request that runs over
// is never followed by another until Tally is free again.
var (
	quickLimit   = 90 * time.Second // one batch of ledgers or stock items
	voucherLimit = 3 * time.Minute  // one month of Day Book; split further on timeout
	slowLimit    = 10 * time.Minute // last-resort whole-collection fallback
	idleWait     = 20 * time.Minute // how long to wait for Tally to finish an abandoned request
	minPause     = 200 * time.Millisecond
	maxPause     = 8 * time.Second
	// While someone is using the computer, requests aim to finish in a few
	// seconds and are followed by an equally long pause; otherwise they can
	// run longer with short pauses.
	busyTarget = 3 * time.Second
	idleTarget = 10 * time.Second
	userBusy   = func() bool { return activity.Busy(time.Minute) }
)

// errSlow means a request hit its time limit; Tally has since become free.
var errSlow = errors.New("tally took too long to answer")

type pacer struct {
	c        *tally.Client
	progress Progress
	stage    string
	frac     float64
	last     time.Duration

	strategies map[string]strategy // per object type, chosen once per run
	slowSeen   int                 // timeouts in the current fetchComputed
}

func (p *pacer) report(stage string, frac float64) {
	p.stage, p.frac = stage, frac
	p.progress(stage, frac)
}

// do runs one Tally request. It first pauses: as long as the previous request
// took while someone is using the computer (Tally is free half the time), a
// quarter of that otherwise. Then it runs fn under limit. On timeout it waits
// for Tally to finish and returns errSlow.
func (p *pacer) do(ctx context.Context, limit time.Duration, what string, fn func(context.Context) error) error {
	if p.last > 0 {
		pause := p.last / 4
		if userBusy() {
			pause = max(p.last, time.Second)
		}
		pause = min(max(pause, minPause), maxPause)
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(pause):
		}
	}
	rctx, cancel := context.WithTimeout(ctx, limit)
	start := time.Now()
	err := fn(rctx)
	timedOut := rctx.Err() == context.DeadlineExceeded
	cancel()
	p.last = time.Since(start)
	log.Printf("tally: %s took %s", what, p.last.Round(10*time.Millisecond))
	if err == nil || !timedOut || ctx.Err() != nil {
		return err
	}
	log.Printf("tally: %s passed its %s limit; waiting for Tally to finish", what, limit)
	p.progress(p.stage+" (Tally is busy, waiting for it to finish)", p.frac)
	if werr := p.c.WaitIdle(ctx, idleWait); werr != nil {
		return werr
	}
	p.last = maxPause * 2 // give Tally a longer breather after a slow request
	p.progress(p.stage, p.frac)
	return errSlow
}

// target is how long one batch should take.
func (p *pacer) target() time.Duration {
	if userBusy() {
		return busyTarget
	}
	return idleTarget
}
