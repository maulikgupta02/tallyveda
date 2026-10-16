package tally

import (
	"context"
	"fmt"
	"net/url"
	"strconv"
	"sync"
	"time"
)

// nearbyPorts is the range FindNearby searches; tests narrow it.
var nearbyPorts = [2]int{9000, 9010}

// FindNearby looks for a Tally with companies loaded on the other local ports
// from 9000 to 9010. TallyPrime on AWS forwards its sync port to localhost,
// and the user may have picked a port other than 9000, or left a Tally that
// is installed on the computer itself holding 9000. It returns a client for
// the first port that has companies, or nil.
func (c *Client) FindNearby(ctx context.Context) (*Client, []Company) {
	u, err := url.Parse(c.base())
	if err != nil || (u.Hostname() != "localhost" && u.Hostname() != "127.0.0.1") {
		return nil, nil
	}
	current, _ := strconv.Atoi(u.Port())
	type hit struct {
		port      int
		client    *Client
		companies []Company
	}
	var (
		mu   sync.Mutex
		hits []hit
		wg   sync.WaitGroup
	)
	for port := nearbyPorts[0]; port <= nearbyPorts[1]; port++ {
		if port == current {
			continue
		}
		wg.Add(1)
		go func(port int) {
			defer wg.Done()
			alt := NewClient(fmt.Sprintf("http://%s:%d", u.Hostname(), port))
			alt.UTF16 = c.UsesUTF16()
			pctx, cancel := context.WithTimeout(ctx, 2*time.Second)
			_, err := alt.Ping(pctx)
			cancel()
			if err != nil {
				return
			}
			if cos, err := alt.Companies(ctx); err == nil && len(cos) > 0 {
				mu.Lock()
				hits = append(hits, hit{port, alt, cos})
				mu.Unlock()
			}
		}(port)
	}
	wg.Wait()
	var best *hit
	for i := range hits {
		if best == nil || hits[i].port < best.port {
			best = &hits[i]
		}
	}
	if best == nil {
		return nil, nil
	}
	return best.client, best.companies
}
