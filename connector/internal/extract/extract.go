package extract

import (
	"context"
	"errors"
	"fmt"
	"math"
	"os"
	"runtime"
	"strings"
	"time"

	"tallyconnector/internal/tally"
)

type Options struct {
	Company    tally.Company
	From, To   time.Time
	ConsentBy  string
	ConsentMsg string
	Version    string
	TallyURL   string
	Banner     string
}

// Progress is called with a human-readable stage and a fraction 0..1.
type Progress func(stage string, fraction float64)

// Master fields are stored on the ledger, so Tally returns them instantly.
// Balances are computed from every voucher in the period, which is the slow
// part, so they are fetched separately in small batches (see batch.go).
var (
	ledgerMasterFetch  = []string{"Name", "Parent", "MasterId", "ReservedName", "IsBillWiseOn", "BillCreditPeriod", "PartyGSTIN", "LedStateName", "StateName", "CountryName"}
	ledgerBalanceFetch = []string{"Name", "Parent", "OpeningBalance", "ClosingBalance"}
	stockValueFetch    = []string{"Name", "Parent", "BaseUnits", "ClosingBalance", "ClosingValue"}
)

func Run(ctx context.Context, c *tally.Client, o Options, progress Progress) (*Bundle, error) {
	host, _ := os.Hostname()
	b := &Bundle{
		SchemaVersion:    SchemaVersion,
		ConnectorVersion: o.Version,
		ExtractedAt:      time.Now().Format(time.RFC3339),
		Consent:          Consent{AcceptedAt: time.Now().Format(time.RFC3339), AcceptedBy: o.ConsentBy, Text: o.ConsentMsg},
		Machine:          Machine{Hostname: host, OS: runtime.GOOS, TallyURL: o.TallyURL, Tally: o.Banner},
		Company:          o.Company,
		Period:           Period{From: o.From.Format("2006-01-02"), To: o.To.Format("2006-01-02")},
		Groups:           []Group{},
		Ledgers:          []Ledger{},
		VoucherTypes:     []Group{},
		StockSnapshots:   []StockSnapshot{},
		Bills:            []Bill{},
		Vouchers:         []Voucher{},
		Warnings:         []string{},
	}
	name := o.Company.Name
	p := &pacer{c: c, progress: progress, strategies: map[string]strategy{}}

	p.report("Reading account groups", 0.02)
	var groups []*tally.Node
	err := p.do(ctx, quickLimit, "groups", func(ctx context.Context) (err error) {
		groups, err = c.Collection(ctx, name, "Group", []string{"Name", "Parent", "ReservedName"}, time.Time{}, time.Time{})
		return err
	})
	if err != nil {
		return nil, fmt.Errorf("reading groups: %w", err)
	}
	for _, n := range groups {
		b.Groups = append(b.Groups, Group{Name: n.ObjectName(), Parent: cleanParent(n.Field("PARENT")), ReservedName: n.Field("RESERVEDNAME")})
	}

	p.report("Reading ledgers", 0.04)
	var masters []*tally.Node
	err = p.do(ctx, slowLimit, "ledger masters", func(ctx context.Context) (err error) {
		masters, err = c.Collection(ctx, name, "Ledger", ledgerMasterFetch, time.Time{}, time.Time{})
		return err
	})
	if err != nil {
		return nil, fmt.Errorf("reading ledgers: %w", err)
	}

	p.report("Reading voucher types", 0.05)
	var vtypes []*tally.Node
	err = p.do(ctx, quickLimit, "voucher types", func(ctx context.Context) (err error) {
		vtypes, err = c.Collection(ctx, name, "VoucherType", []string{"Name", "Parent", "ReservedName"}, time.Time{}, time.Time{})
		return err
	})
	if err != nil {
		b.warn("voucher types could not be read: %v", err)
	}
	for _, n := range vtypes {
		b.VoucherTypes = append(b.VoucherTypes, Group{Name: n.ObjectName(), Parent: cleanParent(n.Field("PARENT")), ReservedName: n.Field("RESERVEDNAME")})
	}

	balances, missing, err := fetchComputed(ctx, p, name, "Ledger", "ledger balances", masters, ledgerBalanceFetch, o.From, o.To, 0.06, 0.20, slowLimit)
	if err != nil {
		return nil, fmt.Errorf("reading ledger balances: %w", err)
	}
	if missing > 0 {
		b.warn("balances of %d ledgers could not be read from Tally", missing)
	}
	for _, n := range masters {
		state := n.Field("LEDSTATENAME")
		if state == "" {
			state = n.Field("STATENAME")
		}
		l := Ledger{
			Name:             n.ObjectName(),
			Parent:           cleanParent(n.Field("PARENT")),
			ReservedName:     n.Field("RESERVEDNAME"),
			IsBillWise:       tally.Bool(n.Field("ISBILLWISEON")),
			CreditPeriodDays: tally.CreditDays(n.Field("BILLCREDITPERIOD")),
			GSTIN:            n.Field("PARTYGSTIN"),
			State:            state,
			Country:          n.Field("COUNTRYNAME"),
		}
		if bal := balances[l.Name]; bal != nil {
			l.OpeningBalance = tally.DebitPositive(bal.Field("OPENINGBALANCE"))
			l.ClosingBalance = tally.DebitPositive(bal.Field("CLOSINGBALANCE"))
		}
		b.Ledgers = append(b.Ledgers, l)
	}

	// Vouchers month by month, split further whenever a range is too slow.
	months := monthChunks(o.From, o.To)
	unbalanced := 0
	for i, ch := range months {
		p.report(fmt.Sprintf("Reading vouchers for %s", ch[0].Format("Jan 2006")), 0.20+0.65*float64(i)/float64(len(months)))
		nodes, err := fetchVouchers(ctx, p, name, ch[0], ch[1])
		if err != nil {
			return nil, fmt.Errorf("reading vouchers %s to %s: %w", ch[0].Format("2006-01-02"), ch[1].Format("2006-01-02"), err)
		}
		for _, n := range nodes {
			v, ok := parseVoucher(n)
			if !ok {
				unbalanced++
			}
			b.Vouchers = append(b.Vouchers, v)
		}
	}
	if unbalanced > 0 {
		b.warn("%d vouchers did not balance when read from Tally", unbalanced)
	}

	// Stock valuation and bills only refine the analysis, so a slow Tally
	// drops them with a warning instead of failing the whole sync.
	readStock(ctx, p, b, o, 0.85, 0.94)

	p.report("Reading outstanding bills", 0.94)
	var bills []*tally.Node
	err = p.do(ctx, voucherLimit, "outstanding bills", func(ctx context.Context) (err error) {
		bills, err = c.Collection(ctx, name, "Bills", []string{"Name", "Parent", "BillDate", "BillDueDate", "ClosingBalance"}, o.From, o.To)
		return err
	})
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		b.warn("bill-wise outstanding could not be read (ageing will use FIFO): %v", err)
	}
	for _, n := range bills {
		amt := tally.DebitPositive(n.Field("CLOSINGBALANCE"))
		if amt == 0 {
			continue
		}
		b.Bills = append(b.Bills, Bill{
			Ledger: n.Field("PARENT"), Name: n.ObjectName(), Date: tally.ISODate(n.Field("BILLDATE")),
			DueDate: tally.ISODate(n.Field("BILLDUEDATE")), ClosingBalance: amt,
		})
	}
	p.report("Extraction complete", 0.98)
	return b, nil
}

// readStock adds closing stock at the period end and one and two years
// earlier, so the backend can adjust cost of goods sold for stock movement.
func readStock(ctx context.Context, p *pacer, b *Bundle, o Options, f0, f1 float64) {
	name := o.Company.Name
	p.report("Reading stock items", f0)
	var items []*tally.Node
	err := p.do(ctx, slowLimit, "stock items", func(ctx context.Context) (err error) {
		items, err = p.c.Collection(ctx, name, "StockItem", []string{"Name", "Parent", "MasterId"}, time.Time{}, time.Time{})
		return err
	})
	if err != nil {
		b.warn("stock items could not be read: %v", err)
		return
	}
	if len(items) == 0 {
		return
	}
	booksFrom, _ := tally.ParseDate(o.Company.BooksFrom)
	dates := []time.Time{o.To, o.To.AddDate(0, 0, -365), o.To.AddDate(0, 0, -730)}
	step := (f1 - f0) / float64(len(dates))
	for i, asOf := range dates {
		if asOf.Before(booksFrom.AddDate(0, 0, -1)) {
			continue
		}
		label := "stock value at " + asOf.Format("02 Jan 2006")
		values, missing, err := fetchComputed(ctx, p, name, "StockItem", label, items, stockValueFetch, o.From, asOf,
			f0+step*float64(i), f0+step*float64(i+1), voucherLimit)
		if err != nil || missing > 0 {
			if err == nil {
				err = fmt.Errorf("%d items missing", missing)
			}
			b.warn("%s could not be read: %v", label, err)
			if ctx.Err() != nil {
				return
			}
			continue // a partial snapshot would understate stock
		}
		snap := StockSnapshot{AsOf: asOf.Format("2006-01-02"), Items: []StockItem{}}
		for _, it := range items {
			n := values[it.ObjectName()]
			snap.Items = append(snap.Items, StockItem{
				Name:         it.ObjectName(),
				Parent:       cleanParent(it.Field("PARENT")),
				Unit:         n.Field("BASEUNITS"),
				ClosingQty:   math.Abs(tally.Amount(n.Field("CLOSINGBALANCE"))),
				ClosingValue: tally.DebitPositive(n.Field("CLOSINGVALUE")),
			})
		}
		b.StockSnapshots = append(b.StockSnapshots, snap)
	}
}

func (b *Bundle) warn(format string, args ...any) {
	b.Warnings = append(b.Warnings, fmt.Sprintf(format, args...))
}

// fetchVouchers reads a date range. A range that is too slow is split in half
// at once; other failures are retried, then split, down to single days.
func fetchVouchers(ctx context.Context, p *pacer, company string, from, to time.Time) ([]*tally.Node, error) {
	var lastErr error
	what := "vouchers " + from.Format("2006-01-02") + " to " + to.Format("2006-01-02")
	for attempt := 0; attempt < 2; attempt++ {
		var nodes []*tally.Node
		err := p.do(ctx, voucherLimit, what, func(ctx context.Context) (err error) {
			nodes, err = p.c.Vouchers(ctx, company, from, to)
			return err
		})
		if err == nil {
			return nodes, nil
		}
		var reqErr *tally.RequestError
		if errors.As(err, &reqErr) || ctx.Err() != nil {
			return nil, err
		}
		lastErr = err
		if errors.Is(err, errSlow) {
			break
		}
		time.Sleep(time.Duration(attempt+1) * 2 * time.Second)
	}
	days := int(to.Sub(from).Hours() / 24)
	if days < 1 {
		return nil, lastErr
	}
	mid := from.AddDate(0, 0, days/2)
	a, err := fetchVouchers(ctx, p, company, from, mid)
	if err != nil {
		return nil, err
	}
	bn, err := fetchVouchers(ctx, p, company, mid.AddDate(0, 0, 1), to)
	if err != nil {
		return nil, err
	}
	return append(a, bn...), nil
}

func monthChunks(from, to time.Time) [][2]time.Time {
	var out [][2]time.Time
	start := from
	for !start.After(to) {
		end := time.Date(start.Year(), start.Month()+1, 1, 0, 0, 0, 0, time.UTC).AddDate(0, 0, -1)
		if end.After(to) {
			end = to
		}
		out = append(out, [2]time.Time{start, end})
		start = end.AddDate(0, 0, 1)
	}
	return out
}

// cleanParent maps Tally's top-level marker ("Primary", after the illegal
// &#4; prefix is stripped) to "".
func cleanParent(p string) string {
	p = strings.TrimSpace(p)
	if strings.EqualFold(p, "primary") {
		return ""
	}
	return p
}

// parseVoucher reads ledger entries from whichever lists this voucher uses.
// Accounting vouchers carry ALLLEDGERENTRIES.LIST; item invoices put party
// and tax ledgers in LEDGERENTRIES.LIST and the sales/purchase ledger inside
// each inventory line's ACCOUNTINGALLOCATIONS.LIST. Some exports contain both
// ALL* and non-ALL* lists with the same entries, so the first combination
// that balances wins. ok is false if none balances.
func parseVoucher(n *tally.Node) (Voucher, bool) {
	v := Voucher{
		GUID:        n.Field("GUID"),
		MasterID:    tally.Int(n.Field("MASTERID")),
		AlterID:     tally.Int(n.Field("ALTERID")),
		Date:        tally.ISODate(n.Field("DATE")),
		Type:        firstNonEmpty(n.Field("VOUCHERTYPENAME"), n.Attrs["VCHTYPE"]),
		Number:      n.Field("VOUCHERNUMBER"),
		Party:       n.Field("PARTYLEDGERNAME"),
		Reference:   n.Field("REFERENCE"),
		Narration:   n.Field("NARRATION"),
		IsCancelled: tally.Bool(n.Field("ISCANCELLED")),
		IsOptional:  tally.Bool(n.Field("ISOPTIONAL")),
		IsInvoice:   tally.Bool(n.Field("ISINVOICE")),
	}
	all := entries(n.ChildrenNamed("ALLLEDGERENTRIES.LIST"))
	plain := entries(n.ChildrenNamed("LEDGERENTRIES.LIST"))
	invAll := allocations(n.ChildrenNamed("ALLINVENTORYENTRIES.LIST"))
	var invOther []Entry
	for _, tag := range []string{"INVENTORYENTRIES.LIST", "INVENTORYENTRIESIN.LIST", "INVENTORYENTRIESOUT.LIST"} {
		invOther = append(invOther, allocations(n.ChildrenNamed(tag))...)
	}
	candidates := [][]Entry{
		concat(all, invAll), concat(plain, invAll), concat(all, invOther), concat(plain, invOther),
		all, plain, concat(all, plain, invAll, invOther),
	}
	for _, c := range candidates {
		if len(c) > 0 && balanced(c) {
			v.Entries = c
			return v, true
		}
	}
	for _, c := range candidates {
		if len(c) > 0 {
			v.Entries = c
			return v, v.IsCancelled // cancelled vouchers legitimately have no amounts
		}
	}
	v.Entries = []Entry{}
	return v, true
}

func entries(nodes []*tally.Node) []Entry {
	var out []Entry
	for _, e := range nodes {
		name := e.Field("LEDGERNAME")
		if name == "" {
			continue
		}
		en := Entry{Ledger: name, Amount: tally.DebitPositive(e.Field("AMOUNT"))}
		for _, bl := range e.ChildrenNamed("BILLALLOCATIONS.LIST") {
			if bn := bl.Field("NAME"); bn != "" {
				en.Bills = append(en.Bills, BillRef{Name: bn, Type: bl.Field("BILLTYPE"), Amount: tally.DebitPositive(bl.Field("AMOUNT"))})
			}
		}
		out = append(out, en)
	}
	return out
}

func allocations(inv []*tally.Node) []Entry {
	var out []Entry
	for _, line := range inv {
		out = append(out, entries(line.ChildrenNamed("ACCOUNTINGALLOCATIONS.LIST"))...)
	}
	return out
}

func concat(lists ...[]Entry) []Entry {
	var out []Entry
	for _, l := range lists {
		out = append(out, l...)
	}
	return out
}

func balanced(es []Entry) bool {
	var s float64
	for _, e := range es {
		s += e.Amount
	}
	return math.Abs(s) < 1.0
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if v != "" {
			return v
		}
	}
	return ""
}
