// TallyConnector: a single executable the borrower runs on the computer where
// Tally is open. It serves a small local web page, reads the books over
// Tally's XML/HTTP port and uploads them to the bank.
//
// On Windows it is built as a GUI-subsystem app (no console window); the UI
// lives in the browser and the process exits shortly after the tab is closed.
//
// Command line (support, testing and the scheduled task):
//
//	TallyConnector.exe -code ABCD-2345 -company "My Co" -consent "A. Kumar" [-monitor]
//	TallyConnector.exe -company "My Co" -dump books.json   (extract only, no upload)
//	TallyConnector.exe -monitor-run                        (scheduled monthly check)
//	TallyConnector.exe -monitor-stop                       (withdraw monthly consent)
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"os/signal"
	"runtime"
	"time"

	"tallyconnector/internal/app"
	"tallyconnector/internal/extract"
	"tallyconnector/internal/monitor"
	"tallyconnector/internal/tally"
	"tallyconnector/internal/upload"
)

// Set at build time: -ldflags "-X main.version=1.0.0 -X main.defaultServer=https://tally.bank.example".
var (
	version       = "dev"
	defaultServer = "http://localhost:8000"
)

func main() {
	tallyURL := flag.String("tally", envOr("TC_TALLY_URL", "http://localhost:9000"), "Tally XML server URL")
	server := flag.String("server", envOr("TC_SERVER", defaultServer), "bank server URL")
	port := flag.Int("port", 0, "local UI port (0 = any free port)")
	noBrowser := flag.Bool("no-browser", false, "don't open the browser")
	code := flag.String("code", "", "headless: one-time code from the bank")
	company := flag.String("company", "", "headless: Tally company name")
	months := flag.Int("months", 0, "headless: months of data (default: as requested by the bank, or 24)")
	consent := flag.String("consent", "", "headless: name of the person consenting to the data share")
	monitoring := flag.Bool("monitor", false, "headless: also opt in to monthly updates (if the bank offers them)")
	dump := flag.String("dump", "", "headless: write the extracted bundle to this JSON file instead of uploading")
	monitorRun := flag.Bool("monitor-run", false, "run the scheduled monthly check")
	monitorStop := flag.Bool("monitor-stop", false, "stop monthly updates and tell the bank")
	utf8 := flag.Bool("utf8", false, "send requests to Tally as UTF-8 instead of UTF-16")
	flag.Parse()

	if len(os.Args) > 1 && !*monitorRun {
		attachConsole() // GUI build: show output when run from a command prompt
	}
	logFile, _ := monitor.OpenLog()
	var logOut io.Writer = os.Stderr
	if logFile != nil {
		defer logFile.Close()
		logOut = io.MultiWriter(logFile, os.Stderr) // file first: stderr may be invalid in the GUI build
	}
	log.SetOutput(logOut)
	log.SetFlags(log.LstdFlags)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()

	switch {
	case *monitorRun:
		if err := monitor.Run(ctx, version, monitor.Logger(logOut)); err != nil {
			log.Printf("monthly update failed: %v", err)
			os.Exit(1)
		}
		return
	case *monitorStop:
		if err := monitor.Stop(ctx); err != nil {
			log.Print(err)
			os.Exit(1)
		}
		log.Print("monthly updates stopped")
		return
	}

	tc := tally.NewClient(*tallyURL)
	tc.UTF16 = !*utf8
	backend := upload.New(*server)

	if *company != "" && (*code != "" || *dump != "") {
		os.Exit(headless(ctx, tc, backend, *code, *company, *months, *consent, *dump, *monitoring))
	}

	a := app.New(tc, backend, version)
	url, err := a.Listen(*port)
	if err != nil {
		log.Fatalf("could not start: %v", err)
	}
	log.Printf("Tally Connector %s: UI at %s", version, url)
	fmt.Println("Your browser should open automatically. If it doesn't, open this address:")
	fmt.Printf("\n    %s\n\n", url)
	if !*noBrowser {
		openBrowser(url)
	}
	tick := time.NewTicker(10 * time.Second)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
			if a.Idle() {
				log.Print("browser tab closed; exiting")
				return
			}
		}
	}
}

func headless(ctx context.Context, tc *tally.Client, backend *upload.Backend, code, company string, months int,
	consent, dump string, monitoring bool) int {
	progress := func(stage string, f float64) { log.Printf("%3.0f%%  %s", f*100, stage) }
	if dump != "" {
		companies, err := tc.Companies(ctx)
		if err != nil {
			log.Print(err)
			return 1
		}
		var co *tally.Company
		for i := range companies {
			if companies[i].Name == company {
				co = &companies[i]
			}
		}
		if co == nil {
			log.Printf("company %q is not open in Tally", company)
			return 1
		}
		if months == 0 {
			months = 24
		}
		from, to := app.Period(*co, months, time.Now())
		banner, _ := tc.Ping(ctx)
		b, err := extract.Run(ctx, tc, extract.Options{Company: *co, From: from, To: to, Version: version,
			ConsentBy: consent, TallyURL: tc.URL, Banner: banner}, progress)
		if err != nil {
			log.Print(err)
			return 1
		}
		f, err := os.Create(dump)
		if err != nil {
			log.Print(err)
			return 1
		}
		defer f.Close()
		enc := json.NewEncoder(f)
		enc.SetIndent("", " ")
		if err := enc.Encode(b); err != nil {
			log.Print(err)
			return 1
		}
		log.Printf("wrote %d vouchers, %d ledgers to %s", len(b.Vouchers), len(b.Ledgers), dump)
		return 0
	}
	if consent == "" {
		log.Print("-consent is required: the name of the person authorising the data share")
		return 2
	}
	summary, err := app.RunJob(ctx, tc, backend, version, app.StartRequest{
		Code: code, Company: company, Months: months, ConsentName: consent, Monitoring: monitoring,
	}, progress)
	if err != nil {
		log.Print(err)
		return 1
	}
	log.Print(summary)
	return 0
}

func openBrowser(url string) {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "windows":
		cmd = exec.Command("rundll32", "url.dll,FileProtocolHandler", url)
	case "darwin":
		cmd = exec.Command("open", url)
	default:
		cmd = exec.Command("xdg-open", url)
	}
	_ = cmd.Start()
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
