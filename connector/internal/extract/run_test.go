package extract

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"

	"tallyconnector/internal/tally"
)

// fakeTally answers one request at a time, like the real server, and is slow
// to compute balances for the "Sundry Debtors" group.
type fakeTally struct {
	mu       sync.Mutex
	slow     time.Duration
	requests []string
}

var childOfRe = regexp.MustCompile(`<CHILDOF>(.*?)</CHILDOF>`)

func (f *fakeTally) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if r.Method == http.MethodGet {
		io.WriteString(w, "<RESPONSE>TallyPrime Server is Running</RESPONSE>")
		return
	}
	raw, _ := io.ReadAll(r.Body)
	body := string(raw)
	ledgers := map[string]string{"Cash": "Cash-in-Hand", "Acme": "Sundry Debtors", "Beta": "Sundry Debtors", "Rent": "Indirect Expenses"}
	ledger := func(name, parent string, balances bool) string {
		s := fmt.Sprintf(`<LEDGER NAME="%s"><PARENT>%s</PARENT>`, name, parent)
		if balances {
			s += `<OPENINGBALANCE>-100.00</OPENINGBALANCE><CLOSINGBALANCE>-250.00</CLOSINGBALANCE>`
		}
		return s + "</LEDGER>"
	}
	var out strings.Builder
	out.WriteString("<ENVELOPE>")
	switch {
	case strings.Contains(body, "<TYPE>Group</TYPE>"):
		f.requests = append(f.requests, "groups")
		out.WriteString(`<GROUP NAME="Sundry Debtors"><PARENT>&#4; Primary</PARENT></GROUP>`)
	case strings.Contains(body, "<TYPE>Ledger</TYPE>"):
		m := childOfRe.FindStringSubmatch(body)
		dated := strings.Contains(body, "SVTODATE")
		switch {
		case m != nil:
			f.requests = append(f.requests, "balances:"+m[1])
			if m[1] == "Sundry Debtors" {
				time.Sleep(f.slow)
			}
			for n, p := range ledgers {
				if p == m[1] {
					out.WriteString(ledger(n, p, true))
				}
			}
		case dated:
			f.requests = append(f.requests, "balances:all")
			for n, p := range ledgers {
				out.WriteString(ledger(n, p, true))
			}
		default:
			f.requests = append(f.requests, "ledger masters")
			for n, p := range ledgers {
				out.WriteString(ledger(n, p, false))
			}
		}
	case strings.Contains(body, "Day Book"):
		f.requests = append(f.requests, "vouchers")
		out.WriteString(`<VOUCHER><DATE>20250405</DATE><VOUCHERTYPENAME>Payment</VOUCHERTYPENAME>
		  <ALLLEDGERENTRIES.LIST><LEDGERNAME>Rent</LEDGERNAME><AMOUNT>-500.00</AMOUNT></ALLLEDGERENTRIES.LIST>
		  <ALLLEDGERENTRIES.LIST><LEDGERNAME>Cash</LEDGERNAME><AMOUNT>500.00</AMOUNT></ALLLEDGERENTRIES.LIST></VOUCHER>`)
	default:
		f.requests = append(f.requests, "other")
	}
	out.WriteString("</ENVELOPE>")
	io.WriteString(w, out.String())
}

func TestRunReadsBalancesPerGroupAndRecoversFromASlowGroup(t *testing.T) {
	oldQuick, oldMin, oldMax := quickLimit, minPause, maxPause
	quickLimit, minPause, maxPause = 200*time.Millisecond, time.Millisecond, 10*time.Millisecond
	defer func() { quickLimit, minPause, maxPause = oldQuick, oldMin, oldMax }()

	f := &fakeTally{slow: 400 * time.Millisecond}
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
	if len(b.Ledgers) != 4 {
		t.Fatalf("ledgers: %+v", b.Ledgers)
	}
	for _, l := range b.Ledgers {
		if l.OpeningBalance != 100 || l.ClosingBalance != 250 {
			t.Errorf("%s balances %v/%v", l.Name, l.OpeningBalance, l.ClosingBalance)
		}
	}
	if len(b.Warnings) != 0 {
		t.Errorf("warnings: %v", b.Warnings)
	}
	if len(b.Vouchers) != 1 {
		t.Errorf("vouchers: %d", len(b.Vouchers))
	}
	got := strings.Join(f.requests, ",")
	for _, want := range []string{"ledger masters", "balances:Cash-in-Hand", "balances:Sundry Debtors", "balances:Indirect Expenses", "balances:all"} {
		if !strings.Contains(got, want) {
			t.Errorf("missing request %q in %s", want, got)
		}
	}
}
