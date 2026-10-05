// TallyVedaDemoSeed fills an empty demo company in Tally with about two and a
// half years of synthetic books (dev/demo_seed_data.py), for testing TallyVeda
// on a PC whose Tally has no data. It is a separate program on purpose: the
// connector itself never writes to Tally.
//
// It only writes to an open company whose name starts with "TallyVeda Demo",
// and only if that company has no vouchers other than ones it imported itself
// (so an interrupted run can be resumed). Tally can't create a company from
// outside, so the user creates the empty company first.
//
//	TallyVedaDemoSeed.exe [-tally http://localhost:9000] [-company "TallyVeda Demo"]
package main

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"context"
	"embed"
	"flag"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"tallyconnector/internal/tally"
)

//go:embed data
var data embed.FS

const (
	prefix      = "TallyVeda Demo"
	booksFrom   = "2024-04-01"
	batchSize   = 200
	defaultName = "TallyVeda Demo"
)

func main() {
	tallyURL := flag.String("tally", "http://localhost:9000", "Tally XML server URL")
	company := flag.String("company", "", "demo company to fill (default: the open company whose name starts with \""+prefix+"\")")
	flag.Parse()
	err := run(context.Background(), *tallyURL, *company)
	if err != nil {
		fmt.Printf("\n%v\n", err)
	}
	fmt.Print("\nPress Enter to close.")
	bufio.NewReader(os.Stdin).ReadString('\n')
	if err != nil {
		os.Exit(1)
	}
}

func run(ctx context.Context, url, name string) error {
	files, err := dataFiles()
	if err != nil {
		return err
	}
	tc := tally.NewClient(url)
	fmt.Printf("Connecting to Tally at %s ...\n", url)
	if _, err := tc.Ping(ctx); err != nil {
		return fmt.Errorf("Tally is not answering at %s. Open TallyPrime and check that F1 > Settings > Connectivity has \"TallyPrime acts as\" set to Server or Both on port 9000.\n(%v)", url, err)
	}
	co, err := pickCompany(ctx, tc, name)
	if err != nil {
		return err
	}
	if co.BooksFrom != "" && co.BooksFrom > booksFrom {
		return fmt.Errorf("%q keeps books from %s, but the demo books start on 1-Apr-2024.\nCreate the demo company again with \"Financial year beginning from\" and \"Books beginning from\" both set to 1-Apr-2024.", co.Name, co.BooksFrom)
	}

	ours := map[string]bool{}
	for _, f := range files {
		if strings.Contains(f, "vouchers") {
			lines, _ := readLines(f)
			for _, l := range lines {
				if g := guidRe.FindStringSubmatch(l); g != nil {
					ours[strings.ToLower(g[1])] = true
				}
			}
		}
	}
	stubs, err := tc.VoucherStubs(ctx, co.Name, time.Date(1990, 1, 1, 0, 0, 0, 0, time.UTC), time.Date(2099, 12, 31, 0, 0, 0, 0, time.UTC), 0)
	if err != nil {
		return fmt.Errorf("could not list the vouchers in %q: %v", co.Name, err)
	}
	done := map[string]bool{}
	for _, s := range stubs {
		g := strings.ToLower(s.GUID)
		if !ours[g] {
			return fmt.Errorf("%q already has vouchers that this program did not create, so nothing was written.\nCreate a new, empty company whose name starts with %q and run this again.", co.Name, prefix)
		}
		done[g] = true
	}
	if len(done) > 0 {
		if len(done) == len(ours) {
			fmt.Printf("%q already has all %d demo vouchers. Nothing to do.\n", co.Name, len(done))
			return nil
		}
		fmt.Printf("%q has %d of the demo vouchers from an earlier run; adding the rest.\n", co.Name, len(done))
	}

	fmt.Printf("Filling %q (books from %s). Keep Tally open; this takes a few minutes.\n\n", co.Name, co.BooksFrom)
	var problems []string
	for _, f := range files {
		lines, err := readLines(f)
		if err != nil {
			return err
		}
		vouchers := strings.Contains(f, "vouchers")
		if vouchers {
			lines = skipDone(lines, done)
		}
		label := strings.TrimSuffix(strings.SplitN(f, "-", 2)[1], ".xml.gz")
		created, errs := 0, 0
		for i := 0; i < len(lines); i += batchSize {
			end := min(i+batchSize, len(lines))
			res, err := importBatch(ctx, tc, co.Name, vouchers, lines[i:end])
			if err != nil {
				return fmt.Errorf("Tally stopped answering while importing %s: %v\nRun this again to continue where it stopped.", label, err)
			}
			created += res.created + res.altered
			errs += res.errors
			for _, m := range res.messages {
				// Masters left over from an earlier run are expected.
				if !vouchers && strings.Contains(strings.ToLower(m), "already exists") {
					errs--
					continue
				}
				if len(problems) < 10 {
					problems = append(problems, label+": "+m)
				}
			}
			if vouchers {
				fmt.Printf("\r  vouchers: %d of %d", end, len(lines))
			}
		}
		if vouchers {
			fmt.Println()
		}
		fmt.Printf("  %-12s %d written", label, created)
		if errs > 0 {
			fmt.Printf(", %d rejected", errs)
		}
		fmt.Println()
	}
	if len(problems) > 0 {
		fmt.Println("\nTally reported:")
		for _, p := range problems {
			fmt.Println("  " + p)
		}
	}
	fmt.Printf("\nDone. Open TallyVeda and share %q.\n", co.Name)
	return nil
}

func pickCompany(ctx context.Context, tc *tally.Client, name string) (*tally.Company, error) {
	cos, err := tc.Companies(ctx)
	if err != nil {
		return nil, fmt.Errorf("could not list the open companies: %v", err)
	}
	var demo []tally.Company
	for _, c := range cos {
		if strings.HasPrefix(strings.ToLower(c.Name), strings.ToLower(prefix)) {
			demo = append(demo, c)
		}
	}
	howTo := fmt.Sprintf("In TallyPrime press Alt+K (Company) > Create and enter:\n"+
		"  Company name:                  %s\n"+
		"  State:                         Maharashtra\n"+
		"  Financial year beginning from: 1-Apr-2024\n"+
		"  Books beginning from:          1-Apr-2024\n"+
		"Accept the rest as they are, keep the company open, and run this again.", defaultName)
	if name != "" {
		if !strings.HasPrefix(strings.ToLower(name), strings.ToLower(prefix)) {
			return nil, fmt.Errorf("this program only fills companies whose name starts with %q, to keep it away from real books", prefix)
		}
		for i := range demo {
			if strings.EqualFold(demo[i].Name, name) {
				return &demo[i], nil
			}
		}
		return nil, fmt.Errorf("%q is not open in Tally.\n%s", name, howTo)
	}
	switch len(demo) {
	case 0:
		return nil, fmt.Errorf("No company whose name starts with %q is open in Tally.\n%s", prefix, howTo)
	case 1:
		return &demo[0], nil
	}
	names := make([]string, len(demo))
	for i, c := range demo {
		names[i] = c.Name
	}
	return nil, fmt.Errorf("Several demo companies are open (%s). Close all but one, or run this with -company \"<name>\".", strings.Join(names, ", "))
}

type result struct {
	created, altered, errors int
	messages                 []string
}

var (
	guidRe = regexp.MustCompile(`<GUID>([^<]+)</GUID>`)
	lineRe = regexp.MustCompile(`(?s)<LINEERROR>(.*?)</LINEERROR>`)
)

// importBatch sends one Import Data request. Tally imports what it can and
// reports the rest as errors, so a bad voucher doesn't stop the batch.
func importBatch(ctx context.Context, tc *tally.Client, company string, vouchers bool, lines []string) (result, error) {
	report := "All Masters"
	if vouchers {
		report = "Vouchers"
	}
	env := "<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER><BODY><IMPORTDATA><REQUESTDESC>" +
		"<REPORTNAME>" + report + "</REPORTNAME><STATICVARIABLES><SVCURRENTCOMPANY>" + xmlEscape(company) +
		"</SVCURRENTCOMPANY></STATICVARIABLES></REQUESTDESC><REQUESTDATA>" + strings.Join(lines, "") +
		"</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"
	ctx, cancel := context.WithTimeout(ctx, 10*time.Minute)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, tc.URL, bytes.NewReader([]byte(env)))
	req.Header.Set("Content-Type", "text/xml;charset=utf-8")
	req.Close = true
	resp, err := tc.HTTP.Do(req)
	if err != nil {
		return result{}, err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return result{}, err
	}
	body := decode(raw)
	r := result{created: field(body, "CREATED"), altered: field(body, "ALTERED"), errors: field(body, "ERRORS")}
	for _, m := range lineRe.FindAllStringSubmatch(body, -1) {
		r.messages = append(r.messages, strings.TrimSpace(m[1]))
	}
	if r.created+r.altered+r.errors == 0 && len(r.messages) == 0 && !strings.Contains(body, "<CREATED>") {
		r.messages = append(r.messages, "unexpected answer: "+truncate(strings.TrimSpace(body), 200))
		r.errors = len(lines)
	}
	return r, nil
}

func field(body, tag string) int {
	m := regexp.MustCompile(`<` + tag + `>\s*(\d+)\s*</` + tag + `>`).FindStringSubmatch(body)
	if m == nil {
		return 0
	}
	n, _ := strconv.Atoi(m[1])
	return n
}

// decode handles Tally answering in UTF-16 even to a UTF-8 request.
func decode(b []byte) string {
	if len(b) >= 2 && b[0] == 0xFF && b[1] == 0xFE {
		b = b[2:]
	} else if !(len(b) >= 4 && b[1] == 0 && b[3] == 0) {
		return string(b)
	}
	var sb strings.Builder
	for i := 0; i+1 < len(b); i += 2 {
		sb.WriteRune(rune(uint16(b[i]) | uint16(b[i+1])<<8))
	}
	return sb.String()
}

func skipDone(lines []string, done map[string]bool) []string {
	if len(done) == 0 {
		return lines
	}
	var out []string
	for _, l := range lines {
		if g := guidRe.FindStringSubmatch(l); g == nil || !done[strings.ToLower(g[1])] {
			out = append(out, l)
		}
	}
	return out
}

func dataFiles() ([]string, error) {
	var files []string
	fs.WalkDir(data, "data", func(p string, d fs.DirEntry, err error) error {
		if err == nil && strings.HasSuffix(p, ".xml.gz") {
			files = append(files, strings.TrimPrefix(p, "data/"))
		}
		return nil
	})
	if len(files) == 0 {
		return nil, fmt.Errorf("this build has no demo data; build it with cmd/demoseed/build.sh")
	}
	sort.Strings(files)
	return files, nil
}

func readLines(name string) ([]string, error) {
	f, err := data.Open("data/" + name)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	z, err := gzip.NewReader(f)
	if err != nil {
		return nil, err
	}
	var out []string
	sc := bufio.NewScanner(z)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	for sc.Scan() {
		if l := strings.TrimSpace(sc.Text()); l != "" {
			out = append(out, l)
		}
	}
	return out, sc.Err()
}

func xmlEscape(s string) string {
	var b strings.Builder
	for _, r := range s {
		switch r {
		case '&':
			b.WriteString("&amp;")
		case '<':
			b.WriteString("&lt;")
		case '>':
			b.WriteString("&gt;")
		default:
			b.WriteRune(r)
		}
	}
	return b.String()
}

func truncate(s string, n int) string {
	if len(s) > n {
		return s[:n] + "..."
	}
	return s
}
