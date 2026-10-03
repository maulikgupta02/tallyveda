package extract

import (
	"context"
	"errors"
	"fmt"
	"log"
	"sort"
	"strings"
	"time"

	"tallyconnector/internal/tally"
)

// Computed fields (balances, stock values) are what make Tally slow, so they
// are read in small batches. How to batch depends on what this Tally honours,
// which is checked with a cheap request first: one that asks only for stored
// fields, so it is fast even if the narrowing is ignored.
type strategy int

const (
	byID    strategy = iota + 1 // MasterId ranges: even batches whatever the group sizes
	byGroup                     // CHILDOF one group at a time
	whole                       // one request for everything
)

func (s strategy) String() string {
	return [...]string{"", "MasterId ranges", "groups", "one request"}[s]
}

// Balance batches start small and never exceed maxBatch objects, so no
// single request asks Tally to compute much.
const maxBatch = 300

type member struct {
	name, parent string
	id           int64
}

func members(masters []*tally.Node) []member {
	out := make([]member, 0, len(masters))
	for _, n := range masters {
		out = append(out, member{n.ObjectName(), strings.TrimSpace(n.Field("PARENT")), tally.Int(n.Field("MASTERID"))})
	}
	return out
}

// fetchComputed reads fetch for every master and returns the nodes by name and
// how many masters are still missing. A batch that times out is halved; a
// single object that still times out is skipped rather than retried as part
// of anything bigger.
func fetchComputed(ctx context.Context, p *pacer, company, objType, label string, masters []*tally.Node,
	fetch []string, from, to time.Time, f0, f1 float64, wholeLimit time.Duration) (map[string]*tally.Node, int, error) {
	ms := members(masters)
	got := map[string]*tally.Node{}
	p.slowSeen = 0
	if len(ms) == 0 {
		return got, 0, nil
	}
	st := p.strategies[objType]
	if st == 0 {
		st = chooseStrategy(ctx, p, company, objType, ms)
		if ctx.Err() != nil {
			return nil, 0, ctx.Err()
		}
		p.strategies[objType] = st
		log.Printf("tally: reading %s by %s", objType, st)
	}
	var err error
	switch st {
	case byID:
		err = batchByID(ctx, p, company, objType, label, ms, fetch, from, to, f0, f1, got)
	case byGroup:
		err = batchByGroup(ctx, p, company, objType, label, ms, fetch, from, to, f0, f1, got)
	}
	if err != nil {
		return nil, 0, err
	}
	missing := countMissing(ms, got)
	// Anything a batch strategy left out without a timeout (no MasterId, items
	// under Primary) is fetched whole; never escalate after a timeout.
	if missing > 0 && (st == whole || p.slowSeen == 0) {
		p.report(fmt.Sprintf("Reading %s (remaining %d)", label, missing), f1)
		var nodes []*tally.Node
		err := p.do(ctx, wholeLimit, label+" (all)", func(ctx context.Context) (err error) {
			nodes, err = p.c.Collection(ctx, company, objType, fetch, from, to)
			return err
		})
		if err != nil {
			return nil, missing, err
		}
		for _, n := range nodes {
			if got[n.ObjectName()] == nil {
				got[n.ObjectName()] = n
			}
		}
		missing = countMissing(ms, got)
	}
	return got, missing, nil
}

func countMissing(ms []member, got map[string]*tally.Node) int {
	n := 0
	for _, m := range ms {
		if got[m.name] == nil {
			n++
		}
	}
	return n
}

// chooseStrategy asks for one object by MasterId, then for one small group by
// CHILDOF, both with stored fields only, and keeps the first that returns
// exactly what was asked for.
func chooseStrategy(ctx context.Context, p *pacer, company, objType string, ms []member) strategy {
	probe := func(q tally.Query, want map[string]bool) bool {
		var nodes []*tally.Node
		err := p.do(ctx, quickLimit, "probe "+objType+" filter", func(ctx context.Context) (err error) {
			nodes, err = p.c.CollectionWhere(ctx, company, objType, q, []string{"Name"}, time.Time{}, time.Time{})
			return err
		})
		if err != nil || len(nodes) != len(want) {
			return false
		}
		for _, n := range nodes {
			if !want[n.ObjectName()] {
				return false
			}
		}
		return true
	}
	for _, m := range ms {
		if m.id > 0 {
			if probe(tally.Query{IDs: [2]int64{m.id, m.id}}, map[string]bool{m.name: true}) {
				return byID
			}
			break
		}
	}
	groups := map[string]map[string]bool{}
	for _, m := range ms {
		if cleanParent(m.parent) == "" {
			continue
		}
		if groups[m.parent] == nil {
			groups[m.parent] = map[string]bool{}
		}
		groups[m.parent][m.name] = true
	}
	smallest := ""
	for g, names := range groups {
		if smallest == "" || len(names) < len(groups[smallest]) {
			smallest = g
		}
	}
	if smallest != "" && probe(tally.Query{ChildOf: smallest}, groups[smallest]) {
		return byGroup
	}
	return whole
}

func batchByID(ctx context.Context, p *pacer, company, objType, label string, ms []member,
	fetch []string, from, to time.Time, f0, f1 float64, got map[string]*tally.Node) error {
	var withID []member
	for _, m := range ms {
		if m.id > 0 {
			withID = append(withID, m)
		}
	}
	sort.Slice(withID, func(i, j int) bool { return withID[i].id < withID[j].id })
	size := 25
	for i := 0; i < len(withID); {
		j := min(i+size, len(withID))
		p.report(fmt.Sprintf("Reading %s (%d of %d)", label, i, len(withID)), f0+(f1-f0)*float64(i)/float64(len(withID)))
		q := tally.Query{IDs: [2]int64{withID[i].id, withID[j-1].id}}
		var nodes []*tally.Node
		err := p.do(ctx, quickLimit, fmt.Sprintf("%s %d-%d", label, i+1, j), func(ctx context.Context) (err error) {
			nodes, err = p.c.CollectionWhere(ctx, company, objType, q, fetch, from, to)
			return err
		})
		switch {
		case ctx.Err() != nil:
			return ctx.Err()
		case errors.Is(err, tally.ErrNotRunning):
			return err
		case err != nil:
			if errors.Is(err, errSlow) {
				p.slowSeen++
			}
			if j-i > 1 {
				size = max(1, (j-i)/2)
			} else {
				log.Printf("tally: skipping %s %q: %v", label, withID[i].name, err)
				i++
			}
			continue
		}
		for _, n := range nodes {
			got[n.ObjectName()] = n
		}
		switch t := p.target(); {
		case p.last < t/3:
			size = min(size*2, maxBatch)
		case p.last > t:
			size = max(1, size/2)
		}
		i = j
	}
	return nil
}

func batchByGroup(ctx context.Context, p *pacer, company, objType, label string, ms []member,
	fetch []string, from, to time.Time, f0, f1 float64, got map[string]*tally.Node) error {
	var parents []string
	seen := map[string]bool{}
	for _, m := range ms {
		if !seen[m.parent] && cleanParent(m.parent) != "" {
			seen[m.parent] = true
			parents = append(parents, m.parent)
		}
	}
	for i, parent := range parents {
		p.report(fmt.Sprintf("Reading %s (%d of %d)", label, i+1, len(parents)), f0+(f1-f0)*float64(i)/float64(len(parents)))
		var nodes []*tally.Node
		err := p.do(ctx, quickLimit, label+" under "+parent, func(ctx context.Context) (err error) {
			nodes, err = p.c.CollectionWhere(ctx, company, objType, tally.Query{ChildOf: parent}, fetch, from, to)
			return err
		})
		switch {
		case ctx.Err() != nil:
			return ctx.Err()
		case errors.Is(err, tally.ErrNotRunning):
			return err
		case errors.Is(err, errSlow):
			p.slowSeen++
			continue
		case err != nil:
			continue
		}
		for _, n := range nodes {
			got[n.ObjectName()] = n
		}
	}
	return nil
}
