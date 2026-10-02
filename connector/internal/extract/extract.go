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

var ledgerFetch = []string{
	"Name", "Parent", "ReservedName", "OpeningBalance", "ClosingBalance", "IsBillWiseOn", "BillCreditPeriod",
	"PartyGSTIN", "LedStateName", "StateName", "CountryName",
}

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

	progress("Reading account groups", 0.02)
	groups, err := c.Collection(ctx, name, "Group", []string{"Name", "Parent", "ReservedName"}, time.Time{}, time.Time{})
	if err != nil {
		return nil, fmt.Errorf("reading groups: %w", err)
	}
	for _, n := range groups {
		b.Groups = append(b.Groups, Group{Name: n.ObjectName(), Parent: cleanParent(n.Field("PARENT")), ReservedName: n.Field("RESERVEDNAME")})
	}

	progress("Reading ledgers", 0.06)
	ledgers, err := c.Collection(ctx, name, "Ledger", ledgerFetch, o.From, o.To)
	if err != nil {
		return nil, fmt.Errorf("reading ledgers: %w", err)
	}
	for _, n := range ledgers {
		state := n.Field("LEDSTATENAME")
		if state == "" {
			state = n.Field("STATENAME")
		}
		b.Ledgers = append(b.Ledgers, Ledger{
			Name:             n.ObjectName(),
			Parent:           cleanParent(n.Field("PARENT")),
			ReservedName:     n.Field("RESERVEDNAME"),
			OpeningBalance:   tally.DebitPositive(n.Field("OPENINGBALANCE")),
			ClosingBalance:   tally.DebitPositive(n.Field("CLOSINGBALANCE")),
			IsBillWise:       tally.Bool(n.Field("ISBILLWISEON")),
			CreditPeriodDays: tally.CreditDays(n.Field("BILLCREDITPERIOD")),
			GSTIN:            n.Field("PARTYGSTIN"),
			State:            state,
			Country:          n.Field("COUNTRYNAME"),
		})
	}

	progress("Reading voucher types", 0.10)
	vtypes, err := c.Collection(ctx, name, "VoucherType", []string{"Name", "Parent", "ReservedName"}, time.Time{}, time.Time{})
	if err != nil {
		b.warn("voucher types could not be read: %v", err)
	}
	for _, n := range vtypes {
		b.VoucherTypes = append(b.VoucherTypes, Group{Name: n.ObjectName(), Parent: cleanParent(n.Field("PARENT")), ReservedName: n.Field("RESERVEDNAME")})
	}

	// Closing stock at the period end and one and two years earlier, so the
	// backend can adjust cost of goods sold for stock movement.
	progress("Reading stock valuation", 0.13)
	booksFrom, _ := tally.ParseDate(o.Company.BooksFrom)
	for _, asOf := range []time.Time{o.To, o.To.AddDate(0, 0, -365), o.To.AddDate(0, 0, -730)} {
		if asOf.Before(booksFrom.AddDate(0, 0, -1)) {
			continue
		}
		items, err := c.Collection(ctx, name, "StockItem", []string{"Name", "Parent", "BaseUnits", "ClosingBalance", "ClosingValue"}, o.From, asOf)
		if err != nil {
			b.warn("stock valuation at %s could not be read: %v", asOf.Format("2006-01-02"), err)
			continue
		}
		snap := StockSnapshot{AsOf: asOf.Format("2006-01-02"), Items: []StockItem{}}
		for _, n := range items {
			snap.Items = append(snap.Items, StockItem{
				Name:         n.ObjectName(),
				Parent:       cleanParent(n.Field("PARENT")),
				Unit:         n.Field("BASEUNITS"),
				ClosingQty:   math.Abs(tally.Amount(n.Field("CLOSINGBALANCE"))),
				ClosingValue: tally.DebitPositive(n.Field("CLOSINGVALUE")),
			})
		}
		b.StockSnapshots = append(b.StockSnapshots, snap)
	}

	progress("Reading outstanding bills", 0.16)
	bills, err := c.Collection(ctx, name, "Bills", []string{"Name", "Parent", "BillDate", "BillDueDate", "ClosingBalance"}, o.From, o.To)
	if err != nil {
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

	// Vouchers month by month: Tally's HTTP server is single-threaded and
	// large responses are slow or fail, so small chunks keep it responsive.
	months := monthChunks(o.From, o.To)
	unbalanced := 0
	for i, ch := range months {
		progress(fmt.Sprintf("Reading vouchers for %s", ch[0].Format("Jan 2006")), 0.18+0.80*float64(i)/float64(len(months)))
		nodes, err := fetchVouchers(ctx, c, name, ch[0], ch[1])
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
	progress("Extraction complete", 0.98)
	return b, nil
}

func (b *Bundle) warn(format string, args ...any) {
	b.Warnings = append(b.Warnings, fmt.Sprintf(format, args...))
}

// fetchVouchers retries a range and, if Tally keeps failing, splits it in
// half down to single days.
func fetchVouchers(ctx context.Context, c *tally.Client, company string, from, to time.Time) ([]*tally.Node, error) {
	var lastErr error
	for attempt := 0; attempt < 2; attempt++ {
		nodes, err := c.Vouchers(ctx, company, from, to)
		if err == nil {
			return nodes, nil
		}
		var reqErr *tally.RequestError
		if errors.As(err, &reqErr) || ctx.Err() != nil {
			return nil, err
		}
		lastErr = err
		time.Sleep(time.Duration(attempt+1) * 2 * time.Second)
	}
	days := int(to.Sub(from).Hours() / 24)
	if days < 1 {
		return nil, lastErr
	}
	mid := from.AddDate(0, 0, days/2)
	a, err := fetchVouchers(ctx, c, company, from, mid)
	if err != nil {
		return nil, err
	}
	bn, err := fetchVouchers(ctx, c, company, mid.AddDate(0, 0, 1), to)
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
