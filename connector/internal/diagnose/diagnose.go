// Package diagnose finds out which request, if any, makes a particular Tally
// stop answering. It sends a ladder of small requests, one field at a time,
// each with a short time limit, and stops as soon as Tally stops responding,
// so the report names the request that froze it.
package diagnose

import (
	"context"
	"fmt"
	"runtime"
	"strings"
	"time"

	"tallyconnector/internal/support"
	"tallyconnector/internal/tally"
)

var (
	stepLimit = 20 * time.Second
	idleLimit = 3 * time.Minute
)

type step struct {
	name string
	run  func(ctx context.Context) (string, error)
}

// Run checks Tally at c and returns a plain-text report. company may be "";
// then the first company Tally lists is used.
// report, if not nil, receives the report so far before and after every step,
// so a check that freezes Tally is still visible if the connector is closed.
func Run(ctx context.Context, c *tally.Client, company, version string, progress func(string, float64), report func(string)) string {
	if report == nil {
		report = func(string) {}
	}
	var out strings.Builder
	fmt.Fprintf(&out, "Tally check by connector %s on %s, %s\nTally at %s\n\n", version, runtime.GOOS, time.Now().Format(time.RFC1123), c.URL)

	banner, err := c.Ping(ctx)
	if err != nil {
		fmt.Fprintf(&out, "FAIL  ping: %v\n", err)
		return out.String()
	}
	fmt.Fprintf(&out, "ok    ping: %s\n", banner)
	companies, err := c.Companies(ctx)
	if err != nil {
		fmt.Fprintf(&out, "FAIL  list companies: %v\n", err)
		return out.String()
	}
	for _, co := range companies {
		fmt.Fprintf(&out, "      company %q books from %s, AltVchId %d\n", co.Name, co.BooksFrom, co.AltVchID)
	}
	var co *tally.Company
	for i := range companies {
		if company == "" || companies[i].Name == company {
			co = &companies[i]
			break
		}
	}
	if co == nil {
		fmt.Fprintf(&out, "FAIL  company %q is not open in Tally\n", company)
		return out.String()
	}
	name := co.Name
	to := time.Now().UTC().Truncate(24 * time.Hour)
	week := to.AddDate(0, 0, -7)

	coll := func(objType string, q tally.Query, fetch []string, from, to time.Time) func(context.Context) (string, error) {
		return func(ctx context.Context) (string, error) {
			nodes, err := c.CollectionWhere(ctx, name, objType, q, fetch, from, to)
			return fmt.Sprintf("%d objects", len(nodes)), err
		}
	}
	var none time.Time
	steps := []step{
		{"groups: Name", coll("Group", tally.Query{}, []string{"Name"}, none, none)},
		{"groups: Name, Parent, ReservedName", coll("Group", tally.Query{}, []string{"Name", "Parent", "ReservedName"}, none, none)},
		{"ledgers: Name", coll("Ledger", tally.Query{}, []string{"Name"}, none, none)},
		{"ledgers: Name, Parent, MasterId", coll("Ledger", tally.Query{}, []string{"Name", "Parent", "MasterId"}, none, none)},
	}
	for _, f := range []string{"ReservedName", "IsBillWiseOn", "BillCreditPeriod", "PartyGSTIN", "LedStateName", "StateName", "CountryName"} {
		steps = append(steps, step{"ledgers: Name, " + f, coll("Ledger", tally.Query{}, []string{"Name", f}, none, none)})
	}
	steps = append(steps,
		step{"voucher types", coll("VoucherType", tally.Query{}, []string{"Name", "Parent", "ReservedName"}, none, none)},
		step{"ledger filter by MasterId (1-50)", coll("Ledger", tally.Query{IDs: [2]int64{1, 50}}, []string{"Name"}, none, none)},
		step{"ledger balances, MasterId 1-50", coll("Ledger", tally.Query{IDs: [2]int64{1, 50}}, []string{"Name", "OpeningBalance", "ClosingBalance"}, week, to)},
		step{"ledger balances, Cash-in-Hand group", coll("Ledger", tally.Query{ChildOf: "Cash-in-Hand"}, []string{"Name", "OpeningBalance", "ClosingBalance"}, week, to)},
		step{"voucher list, last 7 days", func(ctx context.Context) (string, error) {
			s, err := c.VoucherStubs(ctx, name, week, to, 0)
			return fmt.Sprintf("%d vouchers", len(s)), err
		}},
		step{"vouchers changed since AlterId 1", func(ctx context.Context) (string, error) {
			s, err := c.VoucherStubs(ctx, name, week, to, 1)
			return fmt.Sprintf("%d vouchers", len(s)), err
		}},
		step{"Day Book, last 7 days", func(ctx context.Context) (string, error) {
			v, err := c.Vouchers(ctx, name, week, to)
			return fmt.Sprintf("%d vouchers", len(v)), err
		}},
		step{"stock items: Name", coll("StockItem", tally.Query{}, []string{"Name", "Parent", "MasterId"}, none, none)},
		step{"outstanding bills", coll("Bills", tally.Query{}, []string{"Name", "Parent", "ClosingBalance"}, week, to)},
	)

	for i, s := range steps {
		progress("Checking: "+s.name, float64(i+1)/float64(len(steps)+1))
		report(out.String() + "...   " + s.name + ": waiting for Tally\n")
		rctx, cancel := context.WithTimeout(ctx, stepLimit)
		start := time.Now()
		res, err := s.run(rctx)
		timedOut := rctx.Err() == context.DeadlineExceeded
		cancel()
		took := time.Since(start).Round(10 * time.Millisecond)
		switch {
		case timedOut:
			fmt.Fprintf(&out, "HANG  %s: no answer in %s\n", s.name, stepLimit)
			if st := support.Status(); st != "" {
				fmt.Fprintf(&out, "      %s\n", st)
			}
			fmt.Fprintf(&out, "      request sent:\n%s\n", c.LastRequest())
			report(out.String())
			support.OnSlow(s.name, 30*time.Second) // CPU sample and a picture of the Tally window
			if werr := c.WaitIdle(ctx, idleLimit); werr != nil {
				fmt.Fprintf(&out, "\nTally stopped responding after %q and was still busy %s later.\n"+
					"Restart Tally before using it again. This request is the likely cause.\n", s.name, idleLimit)
				return out.String()
			}
			fmt.Fprintf(&out, "      Tally recovered after %s\n", time.Since(start).Round(time.Second))
		case err != nil:
			fmt.Fprintf(&out, "FAIL  %s (%s): %v\n", s.name, took, err)
		default:
			fmt.Fprintf(&out, "ok    %s (%s): %s\n", s.name, took, res)
		}
		report(out.String())
		select {
		case <-ctx.Done():
			return out.String()
		case <-time.After(500 * time.Millisecond):
		}
	}
	out.WriteString("\nAll checks finished.\n")
	return out.String()
}
