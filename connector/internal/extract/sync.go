package extract

import (
	"context"
	"errors"
	"fmt"
	"log"
	"sort"
	"time"

	"tallyconnector/internal/tally"
)

// Plan is the server's answer to "start a sync": what to send this time.
type Plan struct {
	SyncID         string   `json:"sync_id"`
	Mode           string   `json:"mode"` // full | delta
	Period         Period   `json:"period"`
	MonthsDone     []string `json:"months_done"`
	HaveMasters    bool     `json:"have_masters"`
	SinceAlterID   int64    `json:"since_alter_id"`
	LastTo         string   `json:"last_to"`     // end date of the previous sync
	StockDates     []string `json:"stock_dates"` // older stock snapshots the server is missing
	RefreshLedgers []string `json:"refresh_ledgers"`
}

// Sink receives the pieces of a sync. Each call that changes vouchers returns
// the ledgers it affected, whose balances are then re-read.
type Sink interface {
	Masters(ctx context.Context, payload map[string]any) ([]string, error)
	Vouchers(ctx context.Context, from, to time.Time, month string, vs []Voucher) ([]string, error)
	Present(ctx context.Context, from, to time.Time, guids []string) ([]string, error)
	Finish(ctx context.Context) error
}

// Balance is a ledger's opening and closing balance for the sync period.
type Balance struct {
	Name           string  `json:"name"`
	OpeningBalance float64 `json:"opening_balance"`
	ClosingBalance float64 `json:"closing_balance"`
}

// recentDays is how far back a delta looks for deleted vouchers; older
// deletions are caught by the periodic full read.
const recentDays = 92

// Sync sends the company's books to sink as the plan asks. A full read goes
// month by month, newest first, skipping months the server already has, so
// an interrupted first sync resumes where it stopped. A delta sends only
// what changed since the last sync.
func Sync(ctx context.Context, c *tally.Client, o Options, plan Plan, sink Sink, progress Progress) (string, error) {
	from, err1 := time.Parse("2006-01-02", plan.Period.From)
	to, err2 := time.Parse("2006-01-02", plan.Period.To)
	if err1 != nil || err2 != nil {
		return "", fmt.Errorf("server sent an invalid period %v", plan.Period)
	}
	o.From, o.To = from, to
	p := newPacer(c, progress)
	if plan.Mode == "full" {
		return syncFull(ctx, p, o, plan, sink)
	}
	return syncDelta(ctx, p, o, plan, sink)
}

func syncFull(ctx context.Context, p *pacer, o Options, plan Plan, sink Sink) (string, error) {
	name := o.Company.Name
	if !plan.HaveMasters {
		m, err := readMasters(ctx, p, name, 0.02)
		if err != nil {
			return "", err
		}
		balances, missing, err := fetchComputed(ctx, p, name, "Ledger", "ledger balances", m.ledgers, ledgerBalanceFetch, o.From, o.To, 0.06, 0.15, slowLimit)
		if err != nil {
			return "", fmt.Errorf("reading ledger balances: %w", err)
		}
		warns := m.warnings
		if missing > 0 {
			warns = append(warns, fmt.Sprintf("balances of %d ledgers could not be read from Tally", missing))
		}
		p.report("Sending ledgers", 0.15)
		if _, err := sink.Masters(ctx, map[string]any{
			"groups": m.groups, "voucher_types": m.voucherTypes, "ledgers": ledgersWithBalances(m.ledgers, balances), "warnings": warns,
		}); err != nil {
			return "", err
		}
	}

	done := map[string]bool{}
	for _, m := range plan.MonthsDone {
		done[m] = true
	}
	months := monthChunks(o.From, o.To)
	sent, total, unbalanced := 0, 0, 0
	for i := len(months) - 1; i >= 0; i-- {
		ch := months[i]
		key := ch[0].Format("2006-01")
		f := 0.15 + 0.70*float64(len(months)-1-i)/float64(len(months))
		if done[key] {
			continue
		}
		p.report(fmt.Sprintf("Reading vouchers for %s", ch[0].Format("Jan 2006")), f)
		vs, bad, err := readVouchers(ctx, p, name, ch[0], ch[1])
		if err != nil {
			return "", err
		}
		unbalanced += bad
		p.report(fmt.Sprintf("Sending %d vouchers for %s", len(vs), ch[0].Format("Jan 2006")), f)
		if _, err := sink.Vouchers(ctx, ch[0], ch[1], key, vs); err != nil {
			return "", err
		}
		sent++
		total += len(vs)
	}

	if total == 0 && len(plan.MonthsDone) == 0 {
		return "", errors.New("no vouchers were found for this company in the selected period")
	}
	extra, err := readExtras(ctx, p, o, stockDates(o))
	if err != nil {
		return "", err
	}
	if unbalanced > 0 {
		extra["warnings"] = append(extra["warnings"].([]string), fmt.Sprintf("%d vouchers did not balance when read from Tally", unbalanced))
	}
	if _, err := sink.Masters(ctx, extra); err != nil {
		return "", err
	}
	p.report("Finishing", 0.98)
	if err := sink.Finish(ctx); err != nil {
		return "", err
	}
	if len(plan.MonthsDone) > 0 {
		return fmt.Sprintf("Resumed and sent the remaining %d months (%d vouchers).", sent, total), nil
	}
	return fmt.Sprintf("Sent %d months (%d vouchers).", sent, total), nil
}

func readExtras(ctx context.Context, p *pacer, o Options, dates []time.Time) (map[string]any, error) {
	snaps, warns := readStock(ctx, p, o, dates, 0.86, 0.94)
	bills, warn, err := readBills(ctx, p, o, 0.94)
	if err != nil {
		return nil, err
	}
	if warn != "" {
		warns = append(warns, warn)
	}
	extra := map[string]any{"warnings": append([]string{}, warns...)}
	if len(snaps) > 0 {
		extra["stock_snapshots"] = snaps
	}
	if warn == "" {
		extra["bills"] = bills
	}
	return extra, nil
}

func syncDelta(ctx context.Context, p *pacer, o Options, plan Plan, sink Sink) (string, error) {
	name := o.Company.Name
	m, err := readMasters(ctx, p, name, 0.02)
	if err != nil {
		return "", err
	}
	affected := map[string]bool{}
	add := func(names []string) {
		for _, n := range names {
			affected[n] = true
		}
	}
	add(plan.RefreshLedgers)
	newLedgers, err := sink.Masters(ctx, map[string]any{
		"groups": m.groups, "voucher_types": m.voucherTypes, "ledgers": ledgersWithBalances(m.ledgers, nil), "warnings": m.warnings,
	})
	if err != nil {
		return "", err
	}
	add(newLedgers)

	// What changed: skip the voucher scan entirely when Tally's company-wide
	// change number hasn't moved.
	// With no AlterIds to compare (SinceAlterID 0), "changed since 0" would list
	// every voucher; rely on the recent days and the periodic full read instead.
	var changed []tally.VoucherStub
	if plan.SinceAlterID > 0 && (o.Company.AltVchID == 0 || o.Company.AltVchID > plan.SinceAlterID) {
		p.report("Looking for changed vouchers", 0.10)
		err := p.do(ctx, slowLimit, "changed vouchers", func(ctx context.Context) (err error) {
			changed, err = p.c.VoucherStubs(ctx, name, booksStart(o), o.To.AddDate(1, 0, 0), plan.SinceAlterID)
			return err
		})
		if err != nil {
			return "", fmt.Errorf("looking for changed vouchers: %w", err)
		}
	}
	log.Printf("sync: %d vouchers changed since %d", len(changed), plan.SinceAlterID)
	// The last few days are always re-read too: cheap, and new entries still
	// arrive if this Tally ignores the change filter.
	recent := o.To.AddDate(0, 0, -3)
	if last, err := time.Parse("2006-01-02", plan.LastTo); err == nil && last.AddDate(0, 0, -2).Before(recent) {
		recent = last.AddDate(0, 0, -2)
	}
	if recent.Before(o.From) {
		recent = o.From
	}
	for d := recent; !d.After(o.To); d = d.AddDate(0, 0, 1) {
		changed = append(changed, tally.VoucherStub{Date: d})
	}
	ranges := dayRanges(changed)
	count := 0
	for i, r := range ranges {
		p.report(fmt.Sprintf("Reading changed vouchers (%d of %d)", i+1, len(ranges)), 0.15+0.35*float64(i)/float64(len(ranges)))
		vs, _, err := readVouchers(ctx, p, name, r[0], r[1])
		if err != nil {
			return "", err
		}
		got, err := sink.Vouchers(ctx, r[0], r[1], "", vs)
		if err != nil {
			return "", err
		}
		add(got)
		count += len(vs)
	}

	// Deletions leave no trace in change numbers, so compare recent months'
	// voucher lists with the server's.
	recentFrom := o.To.AddDate(0, 0, -recentDays)
	if recentFrom.Before(o.From) {
		recentFrom = o.From
	}
	p.report("Checking for deleted vouchers", 0.50)
	var present []tally.VoucherStub
	err = p.do(ctx, slowLimit, "recent voucher list", func(ctx context.Context) (err error) {
		present, err = p.c.VoucherStubs(ctx, name, recentFrom, o.To, 0)
		return err
	})
	if err != nil {
		return "", fmt.Errorf("listing recent vouchers: %w", err)
	}
	guids := make([]string, 0, len(present))
	for _, s := range present {
		guids = append(guids, s.GUID)
	}
	gone, err := sink.Present(ctx, recentFrom, o.To, guids)
	if err != nil {
		return "", err
	}
	add(gone)

	if len(affected) > 0 {
		var nodes []*tally.Node
		for _, n := range m.ledgers {
			if affected[n.ObjectName()] {
				nodes = append(nodes, n)
			}
		}
		balances, missing, err := fetchComputed(ctx, p, name, "Ledger", "changed ledger balances", nodes, ledgerBalanceFetch, o.From, o.To, 0.55, 0.80, slowLimit)
		if err != nil {
			return "", fmt.Errorf("reading ledger balances: %w", err)
		}
		payload := map[string]any{"balances": balancesOf(balances)}
		if missing > 0 {
			payload["warnings"] = []string{fmt.Sprintf("balances of %d changed ledgers could not be read from Tally", missing)}
		}
		if _, err := sink.Masters(ctx, payload); err != nil {
			return "", err
		}
	}
	var dates []time.Time
	if len(affected) > 0 {
		dates = append(dates, o.To)
	}
	for _, s := range plan.StockDates {
		if d, err := time.Parse("2006-01-02", s); err == nil {
			dates = append(dates, d)
		}
	}
	if len(dates) > 0 {
		extra, err := readExtras(ctx, p, o, dates)
		if err != nil {
			return "", err
		}
		if _, err := sink.Masters(ctx, extra); err != nil {
			return "", err
		}
	}
	p.report("Finishing", 0.98)
	if err := sink.Finish(ctx); err != nil {
		return "", err
	}
	return fmt.Sprintf("Sent %d recent or changed vouchers; %d ledgers updated.", count, len(affected)), nil
}

func balancesOf(nodes map[string]*tally.Node) []Balance {
	out := make([]Balance, 0, len(nodes))
	for name, n := range nodes {
		out = append(out, Balance{Name: name, OpeningBalance: tally.DebitPositive(n.Field("OPENINGBALANCE")),
			ClosingBalance: tally.DebitPositive(n.Field("CLOSINGBALANCE"))})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// booksStart is the widest date range start to ask Tally for: without one,
// voucher collections default to the current financial year.
func booksStart(o Options) time.Time {
	if d, ok := tally.ParseDate(o.Company.BooksFrom); ok {
		return d
	}
	return time.Date(2000, 1, 1, 0, 0, 0, 0, time.UTC)
}

// dayRanges turns the dates of changed vouchers into as few Day Book ranges
// as possible: neighbouring days merge, and no range spans over a month.
func dayRanges(stubs []tally.VoucherStub) [][2]time.Time {
	seen := map[time.Time]bool{}
	var days []time.Time
	for _, s := range stubs {
		d := time.Date(s.Date.Year(), s.Date.Month(), s.Date.Day(), 0, 0, 0, 0, time.UTC)
		if !seen[d] {
			seen[d] = true
			days = append(days, d)
		}
	}
	sort.Slice(days, func(i, j int) bool { return days[i].Before(days[j]) })
	var out [][2]time.Time
	for _, d := range days {
		if n := len(out); n > 0 && d.Sub(out[n-1][1]) <= 24*time.Hour && d.Sub(out[n-1][0]) < 31*24*time.Hour {
			out[n-1][1] = d
			continue
		}
		out = append(out, [2]time.Time{d, d})
	}
	return out
}
