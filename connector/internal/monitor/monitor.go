// Package monitor keeps the connector's per-company settings and runs the
// optional daily refresh. Every company shared from this computer is one
// entry, with its own bank token, so several companies (and banks) can be
// kept up to date from one Tally. When a borrower opts in to daily updates,
// the connector copies itself to the user's AppData folder and registers one
// per-user scheduled task (no admin rights) that serves every entry. Each run
// asks the bank whether an update is due and, if Tally is open with that
// company loaded, syncs it. The bank decides the cadence, so it can change
// without updating the exe.
package monitor

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"runtime"
	"time"

	"tallyconnector/internal/extract"
	"tallyconnector/internal/tally"
	"tallyconnector/internal/upload"
)

type Config struct {
	Server        string `json:"server"`
	Token         string `json:"token"`
	Company       string `json:"company"`
	TallyURL      string `json:"tally_url"`
	BankName      string `json:"bank_name"`
	ApplicantName string `json:"applicant_name"`
	Reference     string `json:"reference"`
	ConsentBy     string `json:"consent_by"`
	ConsentAt     string `json:"consent_at"`
	Months        int    `json:"months,omitempty"`
	// Daily: the borrower opted in to daily updates. Pending: a share was
	// started and hasn't finished; it resumes where it stopped.
	Daily       bool   `json:"daily"`
	Pending     bool   `json:"pending,omitempty"`
	InstalledAt string `json:"installed_at"`
	LastAttempt string `json:"last_attempt,omitempty"`
	LastSuccess string `json:"last_success,omitempty"`
	LastError   string `json:"last_error,omitempty"`
}

// ID identifies an entry to the local UI without exposing its token.
func (c *Config) ID() string {
	sum := sha256.Sum256([]byte(c.Token))
	return hex.EncodeToString(sum[:6])
}

// Dir is where the connector keeps its copy, config and log:
// %AppData%\TallyConnector on Windows. TC_HOME overrides it (tests).
func Dir() (string, error) {
	if d := os.Getenv("TC_HOME"); d != "" {
		return d, os.MkdirAll(d, 0o700)
	}
	base, err := os.UserConfigDir()
	if err != nil {
		return "", err
	}
	d := filepath.Join(base, "TallyConnector")
	return d, os.MkdirAll(d, 0o700)
}

func listPath() (string, error) {
	d, err := Dir()
	return filepath.Join(d, "companies.json"), err
}

// LoadAll returns every saved company. A single-company monitor.json from
// connector 0.1.x is converted on first read.
func LoadAll() ([]*Config, error) {
	p, err := listPath()
	if err != nil {
		return nil, err
	}
	raw, err := os.ReadFile(p)
	if errors.Is(err, os.ErrNotExist) {
		old := filepath.Join(filepath.Dir(p), "monitor.json")
		legacy, lerr := os.ReadFile(old)
		if lerr != nil {
			return nil, nil
		}
		var c Config
		if err := json.Unmarshal(legacy, &c); err != nil {
			return nil, err
		}
		c.Daily = true
		list := []*Config{&c}
		if err := saveAll(list); err != nil {
			return nil, err
		}
		os.Remove(old)
		return list, nil
	}
	if err != nil {
		return nil, err
	}
	var list []*Config
	return list, json.Unmarshal(raw, &list)
}

func saveAll(list []*Config) error {
	p, err := listPath()
	if err != nil {
		return err
	}
	if list == nil {
		list = []*Config{}
	}
	raw, _ := json.MarshalIndent(list, "", "  ")
	tmp := p + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, p)
}

// Find returns the entry with this ID, or nil.
func Find(id string) (*Config, error) {
	list, err := LoadAll()
	for _, c := range list {
		if c.ID() == id {
			return c, err
		}
	}
	return nil, err
}

// Put adds or replaces the entry with c's token.
func Put(c *Config) error {
	list, err := LoadAll()
	if err != nil {
		return err
	}
	for i, x := range list {
		if x.Token == c.Token {
			list[i] = c
			return saveAll(list)
		}
	}
	return saveAll(append(list, c))
}

// Remove deletes the entry with this token, and the scheduled task once no
// entry wants daily updates.
func Remove(token string) error {
	list, err := LoadAll()
	if err != nil {
		return err
	}
	var keep []*Config
	daily := false
	for _, c := range list {
		if c.Token != token {
			keep = append(keep, c)
			daily = daily || c.Daily
		}
	}
	if err := saveAll(keep); err != nil {
		return err
	}
	if !daily {
		return unschedule()
	}
	return nil
}

// Install saves the entry and, for daily updates, copies the running exe to
// Dir() (so the downloaded file can be deleted) and schedules the check. A
// non-nil error means the share works but automatic updates are not set up.
func Install(c *Config) error {
	c.InstalledAt = time.Now().Format(time.RFC3339)
	if err := Put(c); err != nil {
		return fmt.Errorf("saving settings: %w", err)
	}
	if !c.Daily {
		return nil
	}
	exe, err := installCopy()
	if err != nil {
		return fmt.Errorf("copying the connector: %w", err)
	}
	return schedule(exe)
}

// Stop withdraws consent at the bank and removes the entry.
func Stop(ctx context.Context, c *Config) error {
	remoteErr := upload.New(c.Server).MonitorStop(ctx, c.Token)
	if errors.Is(remoteErr, upload.ErrTokenRevoked) {
		remoteErr = nil // already stopped on the bank's side
	}
	if err := Remove(c.Token); err != nil {
		return err
	}
	if remoteErr != nil {
		return fmt.Errorf("daily updates were removed from this computer, but the bank could not be told: %w", remoteErr)
	}
	return nil
}

// Upgrade replaces the installed copy that the scheduled task runs with the
// running exe, so opening a newer connector also updates the daily updates.
func Upgrade() error {
	list, err := LoadAll()
	if err != nil {
		return err
	}
	for _, c := range list {
		if c.Daily {
			_, err := installCopy()
			return err
		}
	}
	return nil
}

// StopAll stops every company's daily updates (the -monitor-stop flag).
func StopAll(ctx context.Context) error {
	list, err := LoadAll()
	if err != nil {
		return err
	}
	var errs []error
	for _, c := range list {
		if c.Daily {
			errs = append(errs, Stop(ctx, c))
		}
	}
	unschedule()
	return errors.Join(errs...)
}

func installCopy() (string, error) {
	self, err := os.Executable()
	if err != nil {
		return "", err
	}
	d, err := Dir()
	if err != nil {
		return "", err
	}
	name := "TallyConnector"
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	dst := filepath.Join(d, name)
	if same(self, dst) {
		return dst, nil
	}
	in, err := os.Open(self)
	if err != nil {
		return "", err
	}
	defer in.Close()
	tmp := dst + ".new"
	out, err := os.OpenFile(tmp, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o755)
	if err != nil {
		return "", err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return "", err
	}
	if err := out.Close(); err != nil {
		return "", err
	}
	return dst, os.Rename(tmp, dst)
}

func same(a, b string) bool {
	sa, err1 := os.Stat(a)
	sb, err2 := os.Stat(b)
	return err1 == nil && err2 == nil && os.SameFile(sa, sb)
}

// OpenLog returns the append-only log in Dir(), trimmed when it grows large.
func OpenLog() (*os.File, error) {
	d, err := Dir()
	if err != nil {
		return nil, err
	}
	p := filepath.Join(d, "connector.log")
	if st, err := os.Stat(p); err == nil && st.Size() > 2<<20 {
		os.Rename(p, p+".old")
	}
	return os.OpenFile(p, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
}

// Run is one scheduled check over every daily entry. It returns nil when
// there was nothing to do or Tally was closed (the next run retries).
func Run(ctx context.Context, version string, logf func(string, ...any)) error {
	list, err := LoadAll()
	if err != nil {
		return err
	}
	var errs []error
	daily := 0
	for _, c := range list {
		if !c.Daily {
			continue
		}
		daily++
		if err := runOne(ctx, c, version, logf); err != nil {
			logf("%s: %v", c.Company, err)
			errs = append(errs, err)
		}
	}
	if daily == 0 {
		logf("daily updates are not set up; removing scheduled task")
		return unschedule()
	}
	return errors.Join(errs...)
}

func runOne(ctx context.Context, c *Config, version string, logf func(string, ...any)) (err error) {
	c.LastAttempt = time.Now().Format(time.RFC3339)
	keep := true // false once removed, so the entry isn't written back
	defer func() {
		if keep {
			Put(c)
		}
	}()

	backend := upload.New(c.Server)
	st, err := backend.MonitorStatus(ctx, c.Token)
	if errors.Is(err, upload.ErrTokenRevoked) || (err == nil && !st.Active) {
		logf("%s: the bank has stopped daily updates; removing them from this computer", c.Company)
		keep = false
		return Remove(c.Token)
	}
	if err != nil {
		c.LastError = err.Error()
		return err
	}
	if !st.Due && !st.Resume {
		logf("%s: no update due (last received %s)", c.Company, st.LastReportAt)
		return nil
	}
	if st.Months > 0 {
		c.Months = st.Months
	}
	summary, err := SyncEntry(ctx, c, version, func(stage string, f float64) { logf("%s: %3.0f%% %s", c.Company, f*100, stage) })
	if errors.Is(err, ErrTallyClosed) {
		c.LastError = err.Error()
		logf("%s: update due but %v; will try again later", c.Company, err)
		return nil
	}
	if err != nil {
		c.LastError = err.Error()
		return err
	}
	c.LastSuccess, c.LastError, c.Pending = time.Now().Format(time.RFC3339), "", false
	logf("%s: %s", c.Company, summary)
	return nil
}

// ErrTallyClosed means Tally isn't open, or the company isn't loaded in it.
var ErrTallyClosed = errors.New("Tally is not open")

// SyncEntry syncs one saved company with its token: it resumes an unfinished
// first share, or sends what changed since the last sync.
func SyncEntry(ctx context.Context, c *Config, version string, progress extract.Progress) (string, error) {
	unlock, err := Lock(c)
	if err != nil {
		return "", err
	}
	defer unlock()
	tc := tally.NewClient(c.TallyURL)
	banner, err := tc.Ping(ctx)
	if err != nil {
		return "", ErrTallyClosed
	}
	companies, err := tc.Companies(ctx)
	if err != nil {
		return "", err
	}
	var co *tally.Company
	for i := range companies {
		if companies[i].Name == c.Company {
			co = &companies[i]
		}
	}
	if co == nil {
		return "", fmt.Errorf("%w with %q loaded", ErrTallyClosed, c.Company)
	}
	months := c.Months
	if months <= 0 {
		months = 24
	}
	from, to := extract.Window(*co, months, time.Now())
	o := extract.Options{Company: *co, From: from, To: to, Version: version, TallyURL: tc.URL, Banner: banner, ConsentBy: c.ConsentBy}
	defer ShipLogs(c.Server, c.Token)()
	backend := upload.New(c.Server)
	st, err := backend.Start(ctx, c.Token, upload.StartRequest{
		Company: co, Period: extract.Period{From: from.Format("2006-01-02"), To: to.Format("2006-01-02")},
		TallyAlterID: co.AltVchID, ConnectorVersion: version, Machine: extract.MachineOf(o),
		Consent: extract.Consent{AcceptedBy: c.ConsentBy, AcceptedAt: c.ConsentAt, MonitoringOptIn: c.Daily,
			Text: fmt.Sprintf("Update under the consent given by %s on %s.", c.ConsentBy, c.ConsentAt)},
	})
	if err != nil {
		return "", err
	}
	return extract.Sync(ctx, tc, o, st.Plan, &upload.Session{B: backend, Token: c.Token, SyncID: st.SyncID}, progress)
}

// Lock keeps two connector processes (the UI and the scheduled task) from
// syncing the same company at once. It is an OS file lock, so it is released
// the moment its process exits, even after a crash or a power cut.
func Lock(c *Config) (func(), error) {
	d, err := Dir()
	if err != nil {
		return nil, err
	}
	release, ok := tryLock(filepath.Join(d, "sync-"+c.ID()+".lock"))
	if !ok {
		return nil, fmt.Errorf("%s is already being synced by another connector window", c.Company)
	}
	return release, nil
}

// LogTail returns up to the last n bytes of connector.log.
func LogTail(n int64) string {
	d, err := Dir()
	if err != nil {
		return ""
	}
	f, err := os.Open(filepath.Join(d, "connector.log"))
	if err != nil {
		return ""
	}
	defer f.Close()
	if st, err := f.Stat(); err == nil && st.Size() > n {
		f.Seek(st.Size()-n, io.SeekStart)
	}
	b, _ := io.ReadAll(f)
	return string(b)
}

// ShipLogs sends the tail of connector.log to the server every minute while a
// sync runs, so support can see where it is even if Tally stops answering.
// The returned func stops it after one last upload.
func ShipLogs(server, token string) func() {
	b := upload.New(server)
	session := fmt.Sprintf("%d", time.Now().Unix())
	send := func() {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		if err := b.SendLog(ctx, upload.TokenAuth(token), "log", LogTail(96<<10), session); err != nil {
			log.Printf("could not send the log to the server: %v", err)
		}
	}
	stop := make(chan struct{})
	done := make(chan struct{})
	go func() {
		defer close(done)
		t := time.NewTicker(time.Minute)
		defer t.Stop()
		for {
			select {
			case <-stop:
				send()
				return
			case <-t.C:
				send()
			}
		}
	}()
	return func() { close(stop); <-done }
}

// Logger returns a printf-style logger writing to w with timestamps.
func Logger(w io.Writer) func(string, ...any) {
	l := log.New(w, "", log.LstdFlags)
	return func(f string, a ...any) { l.Printf(f, a...) }
}
