package extract

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"tallyconnector/internal/tally"
)

// fakeTally answers one request at a time, like the real server. Computing a
// balance costs perItem, so a big unfiltered request is slow.
type fakeTally struct {
	mu          sync.Mutex
	perItem     time.Duration
	honourIDs   bool
	honourChild bool
	ledgers     []fakeLedger
	requests    []string
	maxComputed int
	plComputed  bool // a balance was computed for Profit & Loss A/c
}

type fakeLedger struct {
	name, parent string
	id           int
}

var (
	childOfRe = regexp.MustCompile(`<CHILDOF>(.*?)</CHILDOF>`)
	dateRe    = regexp.MustCompile(`<SVFROMDATE>(\d+)</SVFROMDATE>`)
	toRe      = regexp.MustCompile(`<SVTODATE>(\d+)</SVTODATE>`)
	rangeRe   = regexp.MustCompile(`\$MasterId &gt;= (\d+) AND \$MasterId &lt;= (\d+)`)
)

func newFake(debtors int) *fakeTally {
	f := &fakeTally{perItem: 2 * time.Millisecond, honourIDs: true, honourChild: true}
	f.ledgers = []fakeLedger{{"Cash", "Cash-in-Hand", 1}, {"Rent", "Indirect Expenses", 2}, {"Profit & Loss A/c", "&#4; Primary", 3}}
	for i := 0; i < debtors; i++ {
		f.ledgers = append(f.ledgers, fakeLedger{fmt.Sprintf("Debtor %d", i), "Sundry Debtors", 10 + i})
	}
	return f
}

func (f *fakeTally) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if r.Method == http.MethodGet {
		io.WriteString(w, "<RESPONSE>TallyPrime Server is Running</RESPONSE>")
		return
	}
	raw, _ := io.ReadAll(r.Body)
	body := string(raw)
	var out strings.Builder
	out.WriteString("<ENVELOPE>")
	switch {
	case strings.Contains(body, "<TYPE>Group</TYPE>"):
		f.requests = append(f.requests, "groups")
	case strings.Contains(body, "<TYPE>Ledger</TYPE>"):
		computed := strings.Contains(body, "ClosingBalance")
		kind := "masters"
		match := func(l fakeLedger) bool { return true }
		if m := rangeRe.FindStringSubmatch(body); m != nil && f.honourIDs {
			lo, _ := strconv.Atoi(m[1])
			hi, _ := strconv.Atoi(m[2])
			kind = "range"
			match = func(l fakeLedger) bool { return l.id >= lo && l.id <= hi }
		} else if m := childOfRe.FindStringSubmatch(body); m != nil && f.honourChild {
			kind = "group"
			match = func(l fakeLedger) bool { return l.parent == m[1] }
		} else if computed {
			kind = "all"
		}
		notPL := strings.Contains(body, `NOT $Name = "Profit &amp; Loss A/c"`)
		if computed {
			kind = "balances:" + kind
		} else if strings.Contains(body, "FILTERS") || strings.Contains(body, "CHILDOF") {
			kind = "probe"
		}
		f.requests = append(f.requests, kind)
		n := 0
		for _, l := range f.ledgers {
			if !match(l) || (notPL && l.name == "Profit & Loss A/c") {
				continue
			}
			if computed && l.name == "Profit & Loss A/c" {
				f.plComputed = true
			}
			n++
			fmt.Fprintf(&out, `<LEDGER NAME="%s"><PARENT>%s</PARENT><MASTERID>%d</MASTERID>`, l.name, l.parent, l.id)
			if computed {
				out.WriteString(`<OPENINGBALANCE>-100.00</OPENINGBALANCE><CLOSINGBALANCE>-250.00</CLOSINGBALANCE>`)
			}
			out.WriteString("</LEDGER>")
		}
		if computed {
			f.maxComputed = max(f.maxComputed, n)
			time.Sleep(time.Duration(n) * f.perItem)
		}
	case strings.Contains(body, "Day Book"):
		f.requests = append(f.requests, "vouchers")
		from, to := dateRe.FindStringSubmatch(body), toRe.FindStringSubmatch(body)
		if from == nil || to == nil || from[1] > "20250405" || to[1] < "20250405" {
			break
		}
		out.WriteString(`<VOUCHER><DATE>20250405</DATE><VOUCHERTYPENAME>Payment</VOUCHERTYPENAME>
		  <ALLLEDGERENTRIES.LIST><LEDGERNAME>Rent</LEDGERNAME><AMOUNT>-500.00</AMOUNT></ALLLEDGERENTRIES.LIST>
		  <ALLLEDGERENTRIES.LIST><LEDGERNAME>Cash</LEDGERNAME><AMOUNT>500.00</AMOUNT></ALLLEDGERENTRIES.LIST></VOUCHER>`)
	default:
		f.requests = append(f.requests, "other")
	}
	out.WriteString("</ENVELOPE>")
	io.WriteString(w, out.String())
}

func (f *fakeTally) count(kind string) int {
	n := 0
	for _, r := range f.requests {
		if r == kind {
			n++
		}
	}
	return n
}

func runFake(t *testing.T, f *fakeTally) *Bundle {
	t.Helper()
	oldQuick, oldMin, oldMax, oldBusy := quickLimit, minPause, maxPause, userBusy
	oldBT, oldIT := busyTarget, idleTarget
	quickLimit, minPause, maxPause = 300*time.Millisecond, time.Millisecond, 5*time.Millisecond
	busyTarget, idleTarget = 60*time.Millisecond, 60*time.Millisecond
	userBusy = func() bool { return true }
	defer func() {
		quickLimit, minPause, maxPause, userBusy = oldQuick, oldMin, oldMax, oldBusy
		busyTarget, idleTarget = oldBT, oldIT
	}()

	srv := httptest.NewServer(f)
	defer srv.Close()
	c := tally.NewClient(srv.URL)
	c.UTF16 = false
	from := time.Date(2025, 4, 1, 0, 0, 0, 0, time.UTC)
	b, err := Run(context.Background(), c, Options{Company: tally.Company{Name: "Test"}, From: from, To: from.AddDate(0, 0, 20)},
		func(string, float64) {})
	if err != nil {
		t.Fatal(err)
	}
	if len(b.Ledgers) != len(f.ledgers) {
		t.Fatalf("got %d ledgers, want %d", len(b.Ledgers), len(f.ledgers))
	}
	if f.plComputed {
		t.Fatalf("Profit & Loss A/c balance was requested: %v", f.requests)
	}
	for _, l := range b.Ledgers {
		if l.Name == "Profit & Loss A/c" {
			continue
		}
		if l.OpeningBalance != 100 || l.ClosingBalance != 250 {
			t.Fatalf("%s balances %v/%v", l.Name, l.OpeningBalance, l.ClosingBalance)
		}
	}
	if len(b.Warnings) != 0 || len(b.Vouchers) != 1 {
		t.Fatalf("warnings %v, vouchers %d", b.Warnings, len(b.Vouchers))
	}
	return b
}

// A group of 500 debtors would take a second as one request; ID batches keep
// every request near the target instead.
func TestBalancesAreReadInIDBatchesSizedToTheTarget(t *testing.T) {
	f := newFake(500)
	runFake(t, f)
	if f.count("balances:all") != 0 || f.count("balances:group") != 0 {
		t.Fatalf("expected only ID batches: %v", f.requests)
	}
	if f.maxComputed > 60 {
		t.Errorf("largest batch computed %d ledgers; batches should stay near the 60ms target", f.maxComputed)
	}
}

func TestFallsBackToGroupsWhenIDFilterIsIgnored(t *testing.T) {
	f := newFake(20)
	f.honourIDs = false
	runFake(t, f)
	if f.count("balances:range") != 0 || f.count("balances:all") != 0 || f.count("balances:group") != 3 {
		t.Fatalf("expected one request per group: %v", f.requests)
	}
}

func TestReadsEverythingOnceWhenNoFilterWorks(t *testing.T) {
	f := newFake(20)
	f.honourIDs, f.honourChild = false, false
	runFake(t, f)
	if f.count("balances:all") != 1 {
		t.Fatalf("expected one whole request: %v", f.requests)
	}
}

// A batch that times out is halved until it fits; nothing is escalated to a
// bigger request.
func TestSlowBatchIsHalved(t *testing.T) {
	f := newFake(400)
	f.perItem = 8 * time.Millisecond // 50 ledgers = 400ms, over the 300ms limit
	runFake(t, f)
	if f.count("balances:all") != 0 {
		t.Fatalf("escalated to a whole request: %v", f.requests)
	}
}

// The first ledger request must ask only for stored essentials; optional
// fields, any of which may freeze a Tally, are each asked for separately.
func TestLedgerMastersAskOnlyForEssentials(t *testing.T) {
	for _, f := range optionalLedgerFields {
		for _, m := range ledgerMasterFetch {
			if m == f {
				t.Fatalf("ledgerMasterFetch includes optional field %s", f)
			}
		}
	}
}
