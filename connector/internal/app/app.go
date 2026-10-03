// Package app is the connector's local web UI: a single page served on
// 127.0.0.1 that walks the user through code -> company -> consent -> upload.
package app

import (
	"context"
	"crypto/rand"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"

	"tallyconnector/internal/extract"
	"tallyconnector/internal/monitor"
	"tallyconnector/internal/tally"
	"tallyconnector/internal/upload"
)

//go:embed index.html
var indexHTML string

const (
	ConsentText = "I am authorised to share this company's accounting data. I consent to %s receiving the books of %s " +
		"(ledgers, vouchers, outstanding bills and stock values) for the period %s to %s to assess a credit application."
	MonitoringConsentText = " I also consent to this computer sending an updated copy to %s daily, " +
		"until I or the bank stop it."
)

type App struct {
	Tally   *tally.Client
	Backend *upload.Backend
	Version string

	token    string
	mu       sync.Mutex
	job      Job
	lastPing time.Time
	started  time.Time
}

type Job struct {
	Running  bool    `json:"running"`
	Stage    string  `json:"stage"`
	Fraction float64 `json:"fraction"`
	Error    string  `json:"error,omitempty"`
	Done     bool    `json:"done"`
	Summary  string  `json:"summary,omitempty"`
}

func New(t *tally.Client, b *upload.Backend, version string) *App {
	buf := make([]byte, 16)
	rand.Read(buf)
	return &App{Tally: t, Backend: b, Version: version, token: hex.EncodeToString(buf), started: time.Now()}
}

// Listen starts the UI on a free local port and returns its URL.
func (a *App) Listen(port int) (string, error) {
	ln, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", port))
	if err != nil {
		return "", err
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/", a.index)
	mux.HandleFunc("/api/tally", a.guard(a.tallyStatus))
	mux.HandleFunc("/api/verify", a.guard(a.verify))
	mux.HandleFunc("/api/start", a.guard(a.start))
	mux.HandleFunc("/api/progress", a.guard(a.progress))
	mux.HandleFunc("/api/ping", a.guard(a.ping))
	mux.HandleFunc("/api/monitor", a.guard(a.monitorStatus))
	mux.HandleFunc("/api/monitor/stop", a.guard(a.monitorStop))
	mux.HandleFunc("/api/resume", a.guard(a.resume))
	go http.Serve(ln, mux)
	return fmt.Sprintf("http://%s/?t=%s", ln.Addr().String(), a.token), nil
}

// guard rejects requests without the per-run token and with a foreign Host
// header, so other web pages open in the browser can't drive the connector.
func (a *App) guard(h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		host := r.Host
		if i := strings.LastIndex(host, ":"); i >= 0 {
			host = host[:i]
		}
		if host != "127.0.0.1" && host != "localhost" {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		if r.Header.Get("X-Token") != a.token {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		h(w, r)
	}
}

func (a *App) index(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" || r.URL.Query().Get("t") != a.token {
		http.Error(w, "Open the link shown in the connector window.", http.StatusForbidden)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	page := strings.ReplaceAll(indexHTML, "{{TOKEN}}", a.token)
	page = strings.ReplaceAll(page, "{{VERSION}}", a.Version)
	w.Write([]byte(page))
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

func (a *App) tallyStatus(w http.ResponseWriter, r *http.Request) {
	banner, err := a.Tally.Ping(r.Context())
	if err != nil {
		writeJSON(w, 200, map[string]any{"ok": false, "error": err.Error(), "url": a.Tally.URL})
		return
	}
	companies, err := a.Tally.Companies(r.Context())
	if err != nil {
		writeJSON(w, 200, map[string]any{"ok": false, "banner": banner, "error": err.Error(), "url": a.Tally.URL})
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true, "banner": banner, "companies": companies, "url": a.Tally.URL})
}

func (a *App) verify(w http.ResponseWriter, r *http.Request) {
	var in struct{ Code string }
	json.NewDecoder(r.Body).Decode(&in)
	info, err := a.Backend.Verify(r.Context(), in.Code)
	if err != nil {
		writeJSON(w, 400, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, 200, info)
}

type StartRequest struct {
	Code        string `json:"code"`
	Company     string `json:"company"`
	Months      int    `json:"months"`
	ConsentName string `json:"consent_name"`
	Monitoring  bool   `json:"monitoring"`
}

// Idle reports whether the UI can shut down: no job running and the browser
// page has stopped pinging (tab closed), or it never opened within 10 minutes.
func (a *App) Idle() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.job.Running {
		return false
	}
	if a.lastPing.IsZero() {
		return time.Since(a.started) > 10*time.Minute
	}
	return time.Since(a.lastPing) > 45*time.Second
}

func (a *App) ping(w http.ResponseWriter, r *http.Request) {
	a.mu.Lock()
	a.lastPing = time.Now()
	a.mu.Unlock()
	writeJSON(w, 200, map[string]bool{"ok": true})
}

// monitorStatus lists every company shared from this computer.
func (a *App) monitorStatus(w http.ResponseWriter, r *http.Request) {
	list, _ := monitor.LoadAll()
	out := []map[string]any{}
	for _, c := range list {
		out = append(out, map[string]any{
			"id": c.ID(), "bank_name": c.BankName, "company": c.Company, "daily": c.Daily, "pending": c.Pending,
			"last_success": c.LastSuccess, "last_error": c.LastError, "installed_at": c.InstalledAt,
		})
	}
	writeJSON(w, 200, map[string]any{"companies": out})
}

func (a *App) monitorStop(w http.ResponseWriter, r *http.Request) {
	var in struct{ ID string }
	json.NewDecoder(r.Body).Decode(&in)
	c, err := monitor.Find(in.ID)
	if err != nil || c == nil {
		writeJSON(w, 404, map[string]string{"error": "not found"})
		return
	}
	if err := monitor.Stop(r.Context(), c); err != nil {
		writeJSON(w, 500, map[string]string{"error": err.Error()})
		return
	}
	log.Printf("daily updates for %s stopped by the user", c.Company)
	writeJSON(w, 200, map[string]bool{"stopped": true})
}

// resume continues an interrupted share in the background.
func (a *App) resume(w http.ResponseWriter, r *http.Request) {
	var in struct{ ID string }
	json.NewDecoder(r.Body).Decode(&in)
	c, err := monitor.Find(in.ID)
	if err != nil || c == nil {
		writeJSON(w, 404, map[string]string{"error": "not found"})
		return
	}
	a.run(w, func(ctx context.Context, progress extract.Progress) (string, error) {
		summary, err := monitor.SyncEntry(ctx, c, a.Version, progress)
		if err != nil {
			c.LastError = err.Error()
			monitor.Put(c)
			return "", err
		}
		return finishEntry(c, summary), nil
	})
}

func (a *App) start(w http.ResponseWriter, r *http.Request) {
	var in StartRequest
	if err := json.NewDecoder(r.Body).Decode(&in); err != nil || in.Code == "" || in.Company == "" || strings.TrimSpace(in.ConsentName) == "" {
		writeJSON(w, 400, map[string]string{"error": "code, company and your name are required"})
		return
	}
	a.run(w, func(ctx context.Context, progress extract.Progress) (string, error) {
		return RunJob(ctx, a.Tally, a.Backend, a.Version, in, progress)
	})
}

// run starts job in the background unless one is already running. It keeps
// going if the browser tab is closed.
func (a *App) run(w http.ResponseWriter, job func(context.Context, extract.Progress) (string, error)) {
	a.mu.Lock()
	if a.job.Running {
		a.mu.Unlock()
		writeJSON(w, 409, map[string]string{"error": "already running"})
		return
	}
	a.job = Job{Running: true, Stage: "Starting", Fraction: 0}
	a.mu.Unlock()

	go func() {
		summary, err := job(context.Background(), a.setProgress)
		a.mu.Lock()
		defer a.mu.Unlock()
		a.job.Running = false
		if err != nil {
			a.job.Error = err.Error()
			log.Printf("failed: %v", err)
			return
		}
		a.job.Done, a.job.Fraction, a.job.Stage, a.job.Summary = true, 1, "Done", summary
		log.Printf("done: %s", summary)
	}()
	writeJSON(w, 200, map[string]bool{"started": true})
}

func (a *App) setProgress(stage string, f float64) {
	a.mu.Lock()
	a.job.Stage, a.job.Fraction = stage, f
	a.mu.Unlock()
	log.Printf("%3.0f%%  %s", f*100, stage)
}

func (a *App) progress(w http.ResponseWriter, r *http.Request) {
	a.mu.Lock()
	defer a.mu.Unlock()
	writeJSON(w, 200, a.job)
}

// RunJob verifies the code, opens the first sync and sends the company's
// books. The settings are saved before any data is read, so an interrupted
// share resumes where it stopped (automatically, with daily updates on; from
// the connector page otherwise). Shared by the UI and the command line.
func RunJob(ctx context.Context, t *tally.Client, b *upload.Backend, version string, in StartRequest,
	progress extract.Progress) (string, error) {
	info, err := b.Verify(ctx, in.Code)
	if err != nil {
		return "", err
	}
	banner, err := t.Ping(ctx)
	if err != nil {
		return "", err
	}
	companies, err := t.Companies(ctx)
	if err != nil {
		return "", err
	}
	var company *tally.Company
	for i := range companies {
		if companies[i].Name == in.Company {
			company = &companies[i]
		}
	}
	if company == nil {
		return "", fmt.Errorf("company %q is not open in Tally", in.Company)
	}
	months := in.Months
	if months <= 0 {
		months = info.Months
	}
	from, to := Period(*company, months, time.Now())
	monitoring := in.Monitoring && info.MonitoringOffered
	consent := fmt.Sprintf(ConsentText, info.BankName, company.Name, from.Format("02 Jan 2006"), to.Format("02 Jan 2006"))
	if monitoring {
		consent += fmt.Sprintf(MonitoringConsentText, info.BankName)
	}
	now := time.Now().Format(time.RFC3339)
	o := extract.Options{Company: *company, From: from, To: to, Version: version, TallyURL: t.URL, Banner: banner,
		ConsentBy: strings.TrimSpace(in.ConsentName), ConsentMsg: consent}
	progress("Connecting to "+info.BankName, 0.01)
	st, err := b.StartWithCode(ctx, in.Code, upload.StartRequest{
		Company: company, Period: extract.Period{From: from.Format("2006-01-02"), To: to.Format("2006-01-02")},
		TallyAlterID: company.AltVchID, MonitoringOptIn: monitoring, ConnectorVersion: version, Machine: extract.MachineOf(o),
		Consent: extract.Consent{AcceptedAt: now, AcceptedBy: o.ConsentBy, Text: consent, MonitoringOptIn: monitoring},
	})
	if err != nil {
		return "", err
	}
	c := &monitor.Config{
		Server: b.URL, Token: st.Token, Company: company.Name, TallyURL: t.URL, BankName: info.BankName,
		ApplicantName: info.ApplicantName, Reference: info.Reference, ConsentBy: o.ConsentBy, ConsentAt: now,
		Months: months, Daily: st.Monitoring, Pending: true,
	}
	var setupErr error
	if err := monitor.Install(c); err != nil {
		log.Printf("daily updates: %v", err)
		setupErr = err
	}
	unlock, err := monitor.Lock(c)
	if err != nil {
		return "", err
	}
	defer unlock()
	summary, err := extract.Sync(ctx, t, o, st.Plan, &upload.Session{B: b, Token: st.Token, SyncID: st.SyncID}, progress)
	if err != nil {
		return "", fmt.Errorf("%w. Press \"Continue sharing\" above to carry on from where it stopped", err)
	}
	summary = fmt.Sprintf("Shared %s with %s. %s", company.Name, info.BankName, summary)
	if setupErr != nil {
		return summary + " Daily updates could not be set up automatically (" + setupErr.Error() + ").", nil
	}
	return finishEntry(c, summary), nil
}

// finishEntry records a finished share: a one-off share's entry is removed,
// a daily one is kept for the scheduled task.
func finishEntry(c *monitor.Config, summary string) string {
	if !c.Daily {
		monitor.Remove(c.Token)
		return summary
	}
	c.Pending, c.LastSuccess, c.LastError = false, time.Now().Format(time.RFC3339), ""
	monitor.Put(c)
	return summary + fmt.Sprintf(" Daily updates are on: this computer will send %s what changed each day, "+
		"whenever Tally is open. You can stop this at any time by opening the connector again.", c.BankName)
}

// Period is the extraction window: the last `months` months up to today,
// never before the company's books begin.
func Period(c tally.Company, months int, now time.Time) (time.Time, time.Time) {
	return extract.Window(c, months, now)
}
