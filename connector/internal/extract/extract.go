package extract

import (
	"context"
	"errors"
	"fmt"
	"log"
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
	ledgerMasterFetch  = []string{"Name", "Parent", "MasterId", "ReservedName"}
	ledgerBalanceFetch = []string{"Name", "Parent", "OpeningBalance", "ClosingBalance"}
	stockValueFetch    = []string{"Name", "Parent", "BaseUnits", "ClosingBalance", "ClosingValue"}
)

// Run extracts the whole period in one go and returns it as a single bundle
// (used by -dump; uploads go through Sync).
func Run(ctx context.Context, c *tally.Client, o Options, progress Progress) (*Bundle, error) {
	b := newBundle(o)
	p := newPacer(c, progress)
	m, err := readMasters(ctx, p, o.Company.Name, 0.02)
	if err != nil {
		return nil, err
	}
	b.Groups, b.VoucherTypes, b.Warnings = m.groups, m.voucherTypes, append(b.Warnings, m.warnings...)
	balances, missing, err := fetchComputed(ctx, p, o.Company.Name, "Ledger", "ledger balances", m.ledgers, ledgerBalanceFetch, o.From, o.To, 0.06, 0.20, slowLimit)
	if err != nil {
		return nil, fmt.Errorf("reading ledger balances: %w", err)
	}
	if missing > 0 {
		b.warn("balances of %d ledgers could not be read from Tally", missing)
	}
	b.Ledgers = ledgersWithBalances(m.ledgers, balances)

	months := monthChunks(o.From, o.To)
	unbalanced := 0
	for i, ch := range months {
		p.report(fmt.Sprintf("Reading vouchers for %s", ch[0].Format("Jan 2006")), 0.20+0.65*float64(i)/float64(len(months)))
		vs, bad, err := readVouchers(ctx, p, o.Company.Name, ch[0], ch[1])
		if err != nil {
			return nil, err
		}
		b.Vouchers = append(b.Vouchers, vs...)
		unbalanced += bad
	}
	if unbalanced > 0 {
		b.warn("%d vouchers did not balance when read from Tally", unbalanced)
	}

	// Stock valuation and bills only refine the analysis, so a slow Tally
	// drops them with a warning instead of failing the whole sync.
	snaps, warns := readStock(ctx, p, o, stockDates(o), 0.85, 0.94)
	b.StockSnapshots, b.Warnings = append(b.StockSnapshots, snaps...), append(b.Warnings, warns...)
	bills, warn, err := readBills(ctx, p, o, 0.94)
	if err != nil {
		return nil, err
	}
	b.Bills = append(b.Bills, bills...)
	if warn != "" {
		b.Warnings = append(b.Warnings, warn)
	}
	p.report("Extraction complete", 0.98)
	return b, nil
}

func newBundle(o Options) *Bundle {
	return &Bundle{
		SchemaVersion:    SchemaVersion,
		ConnectorVersion: o.Version,
		ExtractedAt:      time.Now().Format(time.RFC3339),
		Consent:          Consent{AcceptedAt: time.Now().Format(time.RFC3339), AcceptedBy: o.ConsentBy, Text: o.ConsentMsg},
		Machine:          machine(o),
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
}

// Window is the extraction period: the last `months` months up to today,
// never before the company's books begin.
func Window(c tally.Company, months int, now time.Time) (time.Time, time.Time) {
	to := time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, time.UTC)
	from := to.AddDate(0, -months, 1)
	if bf, ok := tally.ParseDate(c.BooksFrom); ok && from.Before(bf) {
		from = bf
	}
	return from, to
}

// MachineOf describes this computer and its Tally for the bundle.
func MachineOf(o Options) Machine { return machine(o) }

func machine(o Options) Machine {
	host, _ := os.Hostname()
	return Machine{Hostname: host, OS: runtime.GOOS, TallyURL: o.TallyURL, Tally: o.Banner}
}

type masters struct {
	groups, voucherTypes []Group
	ledgers              []*tally.Node
	warnings             []string
}

// readMasters reads groups, ledger masters (stored fields, no balances) and
// voucher types: all quick for Tally.
func readMasters(ctx context.Context, p *pacer, company string, f float64) (*masters, error) {
	m := &masters{groups: []Group{}, voucherTypes: []Group{}}
	p.report("Reading account groups", f)
	var groups []*tally.Node
	err := p.do(ctx, quickLimit, "groups", func(ctx context.Context) (err error) {
		groups, err = p.c.Collection(ctx, company, "Group", []string{"Name", "Parent", "ReservedName"}, time.Time{}, time.Time{})
		return err
	})
	if err != nil {
		return nil, fmt.Errorf("reading groups: %w", err)
	}
	for _, n := range groups {
		m.groups = append(m.groups, Group{Name: n.ObjectName(), Parent: cleanParent(n.Field("PARENT")), ReservedName: n.Field("RESERVEDNAME")})
	}

	p.report("Reading ledgers", f+0.02)
	err = p.do(ctx, quickLimit, "ledger masters", func(ctx context.Context) (err error) {
		m.ledgers, err = p.c.Collection(ctx, company, "Ledger", ledgerMasterFetch, time.Time{}, time.Time{})
		return err
	})
	if err != nil {
		return nil, fmt.Errorf("reading ledgers: %w", err)
	}
	if err := readLedgerFields(ctx, p, company, m); err != nil {
		return nil, err
	}

	p.report("Reading voucher types", f+0.03)
	var vtypes []*tally.Node
	err = p.do(ctx, quickLimit, "voucher types", func(ctx context.Context) (err error) {
		vtypes, err = p.c.Collection(ctx, company, "VoucherType", []string{"Name", "Parent", "ReservedName"}, time.Time{}, time.Time{})
		return err
	})
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		m.warnings = append(m.warnings, fmt.Sprintf("voucher types could not be read: %v", err))
	}
	for _, n := range vtypes {
		m.voucherTypes = append(m.voucherTypes, Group{Name: n.ObjectName(), Parent: cleanParent(n.Field("PARENT")), ReservedName: n.Field("RESERVEDNAME")})
	}
	return m, nil
}

// Optional ledger fields are asked for one at a time, after the essentials:
// on some Tally installs one of them makes Tally stop answering (2026-10-04),
// and asking separately pins down which.
var optionalLedgerFields = []string{"IsBillWiseOn", "BillCreditPeriod", "PartyGSTIN", "LedStateName", "StateName", "CountryName"}

var fieldLimit = 30 * time.Second

// FieldMemory, if set, remembers fields that froze this Tally so later runs
// skip them (see monitor.FieldMemory).
var FieldMemory interface {
	Skipped(field string) bool
	Froze(field string)
}

// readLedgerFields adds each optional field to the ledger masters. A field
// that times out is remembered and skipped from then on; the sync carries on
// once Tally answers again.
func readLedgerFields(ctx context.Context, p *pacer, company string, m *masters) error {
	byName := make(map[string]*tally.Node, len(m.ledgers))
	for _, n := range m.ledgers {
		byName[n.ObjectName()] = n
	}
	for _, f := range optionalLedgerFields {
		if FieldMemory != nil && FieldMemory.Skipped(f) {
			m.warnings = append(m.warnings, fmt.Sprintf("ledger field %s skipped: it froze this Tally before", f))
			notify("note", fmt.Sprintf("Skipped ledger detail %s: it froze Tally on an earlier run.", f))
			continue
		}
		var nodes []*tally.Node
		err := p.do(ctx, fieldLimit, "ledger field "+f, func(ctx context.Context) (err error) {
			nodes, err = p.c.Collection(ctx, company, "Ledger", []string{"Name", f}, time.Time{}, time.Time{})
			return err
		})
		var reqErr *tally.RequestError
		if errors.Is(err, errSlow) || errors.As(err, &reqErr) {
			if FieldMemory != nil && errors.Is(err, errSlow) {
				FieldMemory.Froze(f) // Tally recovered, but don't make it wait again
			}
			log.Printf("tally: ledger field %s failed (%v); continuing without it", f, err)
			m.warnings = append(m.warnings, fmt.Sprintf("ledger field %s could not be read: %v", f, err))
			notify("note", fmt.Sprintf("Skipped ledger detail %s: Tally could not provide it (%v).", f, err))
			continue
		}
		if err != nil {
			if FieldMemory != nil && ctx.Err() == nil {
				FieldMemory.Froze(f) // Tally never came back: don't ask again
			}
			return fmt.Errorf("reading ledger field %s: %w. Restart Tally, then continue; this field will be skipped", f, err)
		}
		tag := strings.ToUpper(f)
		for _, n := range nodes {
			if dst := byName[n.ObjectName()]; dst != nil {
				if v := n.Field(tag); v != "" {
					dst.Attrs[tag] = v
				}
			}
		}
	}
	return nil
}

// ledgersWithBalances builds the bundle's ledgers; balances may be nil.
func ledgersWithBalances(nodes []*tally.Node, balances map[string]*tally.Node) []Ledger {
	out := make([]Ledger, 0, len(nodes))
	for _, n := range nodes {
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
		out = append(out, l)
	}
	return out
}

// voucherDays is the most days of Day Book asked for in one request; a range
// that is still slow is split further, down to single days.
var voucherDays = 7

// readVouchers reads and parses [from, to]; bad counts vouchers that didn't balance.
func readVouchers(ctx context.Context, p *pacer, company string, from, to time.Time) (vs []Voucher, bad int, err error) {
	var nodes []*tally.Node
	for start := from; !start.After(to); start = start.AddDate(0, 0, voucherDays) {
		end := start.AddDate(0, 0, voucherDays-1)
		if end.After(to) {
			end = to
		}
		part, err := fetchVouchers(ctx, p, company, start, end)
		if err != nil {
			return nil, 0, fmt.Errorf("reading vouchers %s to %s: %w", start.Format("2006-01-02"), end.Format("2006-01-02"), err)
		}
		nodes = append(nodes, part...)
	}
	vs = make([]Voucher, 0, len(nodes))
	for _, n := range nodes {
		v, ok := parseVoucher(n)
		if !ok {
			bad++
		}
		vs = append(vs, v)
	}
	return vs, bad, nil
}

// stockDates are the period end and one and two years earlier, so the backend
// can adjust cost of goods sold for stock movement.
func stockDates(o Options) []time.Time {
	booksFrom, _ := tally.ParseDate(o.Company.BooksFrom)
	var out []time.Time
	for _, d := range []time.Time{o.To, o.To.AddDate(0, 0, -365), o.To.AddDate(0, 0, -730)} {
		if !d.Before(booksFrom.AddDate(0, 0, -1)) {
			out = append(out, d)
		}
	}
	return out
}

// readStock values every stock item on each date. A date that can't be read
// in full is dropped with a warning: a partial snapshot would understate stock.
func readStock(ctx context.Context, p *pacer, o Options, dates []time.Time, f0, f1 float64) ([]StockSnapshot, []string) {
	var warns []string
	name := o.Company.Name
	p.report("Reading stock items", f0)
	var items []*tally.Node
	err := p.do(ctx, slowLimit, "stock items", func(ctx context.Context) (err error) {
		items, err = p.c.Collection(ctx, name, "StockItem", []string{"Name", "Parent", "MasterId"}, time.Time{}, time.Time{})
		return err
	})
	if err != nil {
		return nil, []string{fmt.Sprintf("stock items could not be read: %v", err)}
	}
	if len(items) == 0 || len(dates) == 0 {
		return nil, nil
	}
	var out []StockSnapshot
	step := (f1 - f0) / float64(len(dates))
	for i, asOf := range dates {
		label := "stock value at " + asOf.Format("02 Jan 2006")
		values, missing, err := fetchComputed(ctx, p, name, "StockItem", label, items, stockValueFetch, o.From, asOf,
			f0+step*float64(i), f0+step*float64(i+1), voucherLimit)
		if err != nil || missing > 0 {
			if err == nil {
				err = fmt.Errorf("%d items missing", missing)
			}
			warns = append(warns, fmt.Sprintf("%s could not be read: %v", label, err))
			notify("note", fmt.Sprintf("Skipped %s: %v.", label, err))
			if ctx.Err() != nil {
				return out, warns
			}
			continue
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
		out = append(out, snap)
	}
	return out, warns
}

// readBills returns bill-wise outstanding. A failure is a warning (the
// backend falls back to FIFO ageing); err is set only when ctx is done.
func readBills(ctx context.Context, p *pacer, o Options, f float64) ([]Bill, string, error) {
	p.report("Reading outstanding bills", f)
	var nodes []*tally.Node
	err := p.do(ctx, voucherLimit, "outstanding bills", func(ctx context.Context) (err error) {
		nodes, err = p.c.Collection(ctx, o.Company.Name, "Bills", []string{"Name", "Parent", "BillDate", "BillDueDate", "ClosingBalance"}, o.From, o.To)
		return err
	})
	if err != nil {
		if ctx.Err() != nil {
			return nil, "", ctx.Err()
		}
		notify("note", fmt.Sprintf("Skipped outstanding bills: %v.", err))
		return nil, fmt.Sprintf("bill-wise outstanding could not be read (ageing will use FIFO): %v", err), nil
	}
	bills := []Bill{}
	for _, n := range nodes {
		amt := tally.DebitPositive(n.Field("CLOSINGBALANCE"))
		if amt == 0 {
			continue
		}
		bills = append(bills, Bill{
			Ledger: n.Field("PARENT"), Name: n.ObjectName(), Date: tally.ISODate(n.Field("BILLDATE")),
			DueDate: tally.ISODate(n.Field("BILLDUEDATE")), ClosingBalance: amt,
		})
	}
	return bills, "", nil
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
