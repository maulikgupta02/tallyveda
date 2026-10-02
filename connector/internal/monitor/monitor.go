// Package monitor implements the optional daily refresh. When the borrower
// opts in, the connector copies itself to the user's AppData folder, saves the
// bank's monitoring token and registers a per-user scheduled task (no admin
// rights). The task runs a few times a day; each run asks the bank whether a
// refresh is due and, if Tally is open, uploads one. The bank decides the
// cadence, so it can change without updating the exe.
package monitor

import (
	"context"
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
	InstalledAt   string `json:"installed_at"`
	LastAttempt   string `json:"last_attempt,omitempty"`
	LastSuccess   string `json:"last_success,omitempty"`
	LastError     string `json:"last_error,omitempty"`
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

func configPath() (string, error) {
	d, err := Dir()
	return filepath.Join(d, "monitor.json"), err
}

// Load returns the saved config, or nil if monitoring isn't set up.
func Load() (*Config, error) {
	p, err := configPath()
	if err != nil {
		return nil, err
	}
	raw, err := os.ReadFile(p)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var c Config
	return &c, json.Unmarshal(raw, &c)
}

func (c *Config) Save() error {
	p, err := configPath()
	if err != nil {
		return err
	}
	raw, _ := json.MarshalIndent(c, "", "  ")
	tmp := p + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, p)
}

// Install saves the config, copies the running exe to Dir() (so the
// downloaded file can be deleted) and schedules the daily check. A non-nil
// error means the upload succeeded but automatic refreshes are not set up.
func Install(c *Config) error {
	c.InstalledAt = time.Now().Format(time.RFC3339)
	if err := c.Save(); err != nil {
		return fmt.Errorf("saving settings: %w", err)
	}
	exe, err := installCopy()
	if err != nil {
		return fmt.Errorf("copying the connector: %w", err)
	}
	if err := schedule(exe, c.BankName); err != nil {
		return err
	}
	return nil
}

// Uninstall removes the scheduled task and settings. The bank is told
// separately (see Stop) when the client withdraws consent.
func Uninstall() error {
	err := unschedule()
	if p, e := configPath(); e == nil {
		os.Remove(p)
	}
	return err
}

// Stop withdraws consent at the bank and removes the local setup.
func Stop(ctx context.Context) error {
	c, err := Load()
	if err != nil || c == nil {
		unschedule()
		return err
	}
	remoteErr := upload.New(c.Server).MonitorStop(ctx, c.Token)
	if errors.Is(remoteErr, upload.ErrTokenRevoked) {
		remoteErr = nil // already stopped on the bank's side
	}
	if err := Uninstall(); err != nil {
		return err
	}
	if remoteErr != nil {
		return fmt.Errorf("daily updates were removed from this computer, but the bank could not be told: %w", remoteErr)
	}
	return nil
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

// Run is one scheduled check. It returns nil when there was nothing to do or
// Tally was closed (the next run retries); errors are for real failures.
func Run(ctx context.Context, version string, logf func(string, ...any)) error {
	c, err := Load()
	if err != nil {
		return err
	}
	if c == nil {
		logf("daily updates are not set up; removing scheduled task")
		return unschedule()
	}
	c.LastAttempt = time.Now().Format(time.RFC3339)
	keep := true // false once uninstalled, so the config isn't written back
	defer func() {
		if keep {
			c.Save()
		}
	}()

	backend := upload.New(c.Server)
	st, err := backend.MonitorStatus(ctx, c.Token)
	if errors.Is(err, upload.ErrTokenRevoked) || (err == nil && !st.Active) {
		logf("the bank has stopped daily updates; removing them from this computer")
		keep = false
		return Uninstall()
	}
	if err != nil {
		c.LastError = err.Error()
		return err
	}
	if !st.Due {
		logf("no update due (last received %s)", st.LastReportAt)
		return nil
	}

	tc := tally.NewClient(c.TallyURL)
	banner, err := tc.Ping(ctx)
	if err != nil {
		c.LastError = "Tally was not open"
		logf("update due but Tally is not open; will try again later")
		return nil
	}
	companies, err := tc.Companies(ctx)
	if err != nil {
		c.LastError = err.Error()
		return err
	}
	var co *tally.Company
	for i := range companies {
		if companies[i].Name == c.Company {
			co = &companies[i]
		}
	}
	if co == nil {
		c.LastError = fmt.Sprintf("company %q was not open in Tally", c.Company)
		logf("update due but %q is not open in Tally; will try again later", c.Company)
		return nil
	}

	now := time.Now()
	to := time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, time.UTC)
	from := to.AddDate(0, -st.Months, 1)
	if bf, ok := tally.ParseDate(co.BooksFrom); ok && from.Before(bf) {
		from = bf
	}
	bundle, err := extract.Run(ctx, tc, extract.Options{
		Company: *co, From: from, To: to, Version: version, TallyURL: tc.URL, Banner: banner,
		ConsentBy:  c.ConsentBy,
		ConsentMsg: fmt.Sprintf("Daily update under the consent given by %s on %s.", c.ConsentBy, c.ConsentAt),
	}, func(stage string, f float64) { logf("%3.0f%% %s", f*100, stage) })
	if err != nil {
		c.LastError = err.Error()
		return err
	}
	bundle.Consent.MonitoringOptIn = true
	if err := backend.MonitorUpload(ctx, c.Token, bundle); err != nil {
		c.LastError = err.Error()
		return err
	}
	c.LastSuccess, c.LastError = time.Now().Format(time.RFC3339), ""
	logf("sent daily update: %d vouchers to %s", len(bundle.Vouchers), c.BankName)
	return nil
}

// Logger returns a printf-style logger writing to w with timestamps.
func Logger(w io.Writer) func(string, ...any) {
	l := log.New(w, "", log.LstdFlags)
	return func(f string, a ...any) { l.Printf(f, a...) }
}
