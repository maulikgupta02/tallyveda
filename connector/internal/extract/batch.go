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
	name, parent, reserved string
	id                     int64
}

func members(masters []*tally.Node) []member {
	out := make([]member, 0, len(masters))
	for _, n := range masters {
		out = append(out, member{n.ObjectName(), strings.TrimSpace(n.Field("PARENT")), n.Field("RESERVEDNAME"), tally.Int(n.Field("MASTERID"))})
	}
	return out
}

// balanceLimit is the time limit for one batch of balances.
var balanceLimit = 30 * time.Second

// neverCompute reports ledgers whose balance Tally can only produce by working
// out the whole profit and loss, closing stock included. On a real install
// (2026-10-04) asking for it froze Tally for over a minute, and the analysis
// derives net worth from assets and liabilities instead.
func neverCompute(m member) bool {
	return m.name == "Profit & Loss A/c" || m.reserved == "Profit & Loss A/c"
}

func skipKey(objType string, m member) string { return objType + ":" + m.name }

// fetchComputed reads fetch for every master and returns the nodes by name and
// how many masters are still missing. A batch that times out is halved; a
// single object that still times out is skipped rather than retried as part
// of anything bigger.
func fetchComputed(ctx context.Context, p *pacer, company, objType, label string, masters []*tally.Node,
	fetch []string, from, to time.Time, f0, f1 float64, wholeLimit time.Duration) (map[string]*tally.Node, int, error) {
	all := members(masters)
	var ms []member
	for _, m := range all {
		switch {
		case objType == "Ledger" && neverCompute(m):
			log.Printf("tally: not asking for the balance of %q (Tally computes it from the whole books)", m.name)
		case FieldMemory != nil && FieldMemory.Skipped(skipKey(objType, m)):
			notify("note", fmt.Sprintf("Skipped %s for %s: it froze Tally on an earlier run.", label, m.name))
		default:
			ms = append(ms, m)
		}
	}
	// A MasterId range or a group can still contain the never-compute ledgers,
	// so every request leaves them out by name.
	var exclude []string
	var excludeIDs []int64
	for _, m := range all {
		if objType == "Ledger" && neverCompute(m) {
			exclude = append(exclude, m.name)
			excludeIDs = append(excludeIDs, m.id)
		}
	}
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
		err = batchByID(ctx, p, company, objType, label, ms, fetch, from, to, f0, f1, got, excludeIDs)
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
		// The one request that relies on a name filter to leave out the
		// never-compute ledgers. If this Tally rejects the filter, go without
		// these balances rather than risk the freeze.
		q := tally.Query{NotNames: exclude}
		var nodes []*tally.Node
		err := p.do(ctx, wholeLimit, label+" (all)", func(ctx context.Context) (err error) {
			nodes, err = p.c.CollectionWhere(ctx, company, objType, q, fetch, from, to)
			return err
		})
		var reqErr *tally.RequestError
		if len(exclude) > 0 && errors.As(err, &reqErr) {
			notify("note", fmt.Sprintf("Skipped %s: this Tally could not leave out %s (%v).", label, strings.Join(exclude, ", "), err))
			return got, missing, nil
		}
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
	// fetch probes with stored fields only, so they are fast even if the
	// narrowing is ignored. ok decides from what came back.
	probe := func(label string, q tally.Query, ok func(nodes []*tally.Node) bool) bool {
		var nodes []*tally.Node
		err := p.do(ctx, quickLimit, "probe "+objType+" "+label, func(ctx context.Context) (err error) {
			nodes, err = p.c.CollectionWhere(ctx, company, objType, q, []string{"Name", "Parent", "MasterId"}, time.Time{}, time.Time{})
			return err
		})
		good := err == nil && ok(nodes)
		log.Printf("tally: probe %s %s: %d objects, err %v, usable %v", objType, label, len(nodes), err, good)
		return good
	}
	names := func(nodes []*tally.Node) map[string]bool {
		out := map[string]bool{}
		for _, n := range nodes {
			out[n.ObjectName()] = true
		}
		return out
	}
	for _, m := range ms {
		if m.id <= 0 {
			continue
		}
		lo, hi := m.id, m.id
		if probe(fmt.Sprintf("MasterId %d", m.id), tally.Query{IDs: [2]int64{lo, hi}}, func(nodes []*tally.Node) bool {
			for _, n := range nodes {
				if id := tally.Int(n.Field("MASTERID")); id != 0 && (id < lo || id > hi) {
					return false // filter ignored
				}
			}
			return names(nodes)[m.name] && len(nodes) < len(ms)+1
		}) {
			return byID
		}
		break
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
	for g, members := range groups {
		if smallest == "" || len(members) < len(groups[smallest]) {
			smallest = g
		}
	}
	if smallest != "" && probe("group "+smallest, tally.Query{ChildOf: smallest}, func(nodes []*tally.Node) bool {
		got := names(nodes)
		for n := range groups[smallest] {
			if !got[n] {
				return false
			}
		}
		// Sub-group ledgers may come back too; everything coming back means ignored.
		return len(nodes) < len(ms) || len(groups[smallest]) == len(ms)
	}) {
		return byGroup
	}
	return whole
}

func batchByID(ctx context.Context, p *pacer, company, objType, label string, ms []member,
	fetch []string, from, to time.Time, f0, f1 float64, got map[string]*tally.Node, excludeIDs []int64) error {
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
		// A range must not span a never-compute ledger's MasterId.
		for _, x := range excludeIDs {
			for k := i + 1; k < j; k++ {
				if withID[k-1].id < x && x < withID[k].id {
					j = k
					break
				}
			}
		}
		p.report(fmt.Sprintf("Reading %s (%d of %d)", label, i, len(withID)), f0+(f1-f0)*float64(i)/float64(len(withID)))
		q := tally.Query{IDs: [2]int64{withID[i].id, withID[j-1].id}}
		var nodes []*tally.Node
		err := p.do(ctx, balanceLimit, fmt.Sprintf("%s %d-%d", label, i+1, j), func(ctx context.Context) (err error) {
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
				if errors.Is(err, errSlow) && FieldMemory != nil {
					FieldMemory.Froze(skipKey(objType, withID[i]))
				}
				notify("note", fmt.Sprintf("Skipped %s for %s: Tally took too long.", label, withID[i].name))
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
		err := p.do(ctx, balanceLimit, label+" under "+parent, func(ctx context.Context) (err error) {
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
