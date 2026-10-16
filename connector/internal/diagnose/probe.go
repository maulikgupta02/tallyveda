package diagnose

import (
	"context"
	"fmt"
	"strings"
	"time"

	"tallyconnector/internal/support"
	"tallyconnector/internal/tally"
)

// Probe is a deeper look for telemetry when no company can be found. It lists
// what answers on local ports 9000-9010 and who owns them, and tries each way
// of asking for the company list, recording only the outline of each reply.
func Probe(ctx context.Context, address string) string {
	var b strings.Builder
	fmt.Fprintf(&b, "Tally address: %s\n", address)
	if local := support.LocalTally(); len(local) > 0 {
		fmt.Fprintf(&b, "Tally running on this computer: %s\n", strings.Join(local, "; "))
	} else {
		b.WriteString("No Tally process on this computer\n")
	}
	b.WriteString("Local ports:\n")
	for port := 9000; port <= 9010; port++ {
		c := tally.NewClient(fmt.Sprintf("http://localhost:%d", port))
		pctx, cancel := context.WithTimeout(ctx, 3*time.Second)
		banner, err := c.Ping(pctx)
		cancel()
		owner := support.PortOwner(port)
		switch {
		case err == nil:
			fmt.Fprintf(&b, "  %d: answers %q; owner %s\n", port, banner, orNone(owner))
		case owner != "":
			fmt.Fprintf(&b, "  %d: listening (owner %s) but ping failed: %s\n", port, owner, short(err))
		case port == 9000 || strings.Contains(address, fmt.Sprintf(":%d", port)):
			fmt.Fprintf(&b, "  %d: nothing answers (%s)\n", port, short(err))
		}
	}
	b.WriteString("Company list requests at " + address + ":\n")
	for _, v := range tally.CompanyRequests() {
		c := tally.NewClient(address)
		c.UTF16 = v.UTF16
		start := time.Now()
		rctx, cancel := context.WithTimeout(ctx, 60*time.Second)
		n, err := v.Run(rctx, c)
		cancel()
		took := time.Since(start).Round(10 * time.Millisecond)
		if err != nil {
			fmt.Fprintf(&b, "  %s: error after %s: %s; %s\n", v.Name, took, short(err), c.LastOutline())
			continue
		}
		fmt.Fprintf(&b, "  %s: %d companies in %s; %s\n", v.Name, n, took, c.LastOutline())
	}
	return b.String()
}

func orNone(s string) string {
	if s == "" {
		return "unknown"
	}
	return s
}

func short(err error) string {
	s := err.Error()
	if len(s) > 200 {
		s = s[:200] + "…"
	}
	return s
}
