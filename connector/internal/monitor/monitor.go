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
	"strings"
	"sync"
	"time"

	"tallyconnector/internal/activity"
	"tallyconnector/internal/extract"
	"tallyconnector/internal/support"
	"tallyconnector/internal/tally"
	"tallyconnector/internal/update"
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

// sameShare reports whether two entries share the same company with the same
// requester, so the newer one (a fresh access code) supersedes the older.
func sameShare(a, b *Config) bool {
	return a.Server == b.Server && a.Company == b.Company && a.BankName == b.BankName
}

// Put adds or replaces the entry with c's token. A new entry replaces older
// ones for the same company and requester: trying again with a new code would
// otherwise leave one "Continue sharing" entry per attempt.
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
	keep := []*Config{}
	for _, x := range list {
		if !sameShare(x, c) {
			keep = append(keep, x)
		}
	}
	return saveAll(append(keep, c))
}

// Prune drops entries the server no longer knows (the request was deleted or
// its updates stopped) and, of duplicates for the same company and requester,
// keeps only the most recently installed. Entries it can't check (offline) stay.
func Prune(ctx context.Context) error {
	list, err := LoadAll()
	if err != nil || len(list) == 0 {
		return err
	}
	gone := make([]bool, len(list))
	var wg sync.WaitGroup
	for i, c := range list {
		wg.Add(1)
		go func(i int, c *Config) {
			defer wg.Done()
			st, err := upload.New(c.Server).MonitorStatus(ctx, c.Token, upload.Heartbeat{Version: Version})
			gone[i] = errors.Is(err, upload.ErrTokenRevoked) || (err == nil && !st.Active && !c.Pending)
		}(i, c)
	}
	wg.Wait()
	keep := []*Config{}
	for i, c := range list {
		if gone[i] {
			continue
		}
		dup := -1
		for j, k := range keep {
			if sameShare(k, c) {
				dup = j
			}
		}
		switch {
		case dup < 0:
			keep = append(keep, c)
		case c.InstalledAt > keep[dup].InstalledAt:
			keep[dup] = c
		}
	}
	if len(keep) == len(list) {
		return nil
	}
	daily := false
	for _, c := range keep {
		daily = daily || c.Daily
	}
	if err := saveAll(keep); err != nil {
		return err
	}
	if !daily {
		return unschedule()
	}
	return nil
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
// running exe and re-registers the task, so opening a newer connector also
// updates the daily updates and their schedule.
func Upgrade() error {
	list, err := LoadAll()
	if err != nil {
		return err
	}
	for _, c := range list {
		if c.Daily {
			exe, err := installCopy()
			if err != nil || runtime.GOOS != "windows" {
				return err
			}
			return schedule(exe)
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

// Version is the running connector's version, set by main. The installed copy
// is never replaced by an older one (someone opening an old download after an
// update).
var Version = "dev"

func installedPath() (string, error) {
	d, err := Dir()
	if err != nil {
		return "", err
	}
	name := "TallyConnector"
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	return filepath.Join(d, name), nil
}

// InstalledVersion is the version of the copy the scheduled task runs.
func InstalledVersion() string {
	d, err := Dir()
	if err != nil {
		return ""
	}
	b, _ := os.ReadFile(filepath.Join(d, "installed-version"))
	return strings.TrimSpace(string(b))
}

func setInstalledVersion(v string) {
	if d, err := Dir(); err == nil {
		os.WriteFile(filepath.Join(d, "installed-version"), []byte(v), 0o600)
	}
}

func installCopy() (string, error) {
	self, err := os.Executable()
	if err != nil {
		return "", err
	}
	dst, err := installedPath()
	if err != nil {
		return "", err
	}
	if same(self, dst) {
		return dst, nil
	}
	if _, err := os.Stat(dst); err == nil && update.Newer(InstalledVersion(), Version) {
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
	if err := os.Rename(tmp, dst); err != nil {
		return "", err
	}
	setInstalledVersion(Version)
	return dst, nil
}

// AutoUpdate installs a newer release over the copy the scheduled task runs,
// when the server allows background updates. The current run carries on with
// the old code; the next one uses the new version.
func AutoUpdate(ctx context.Context, logf func(string, ...any)) {
	list, err := LoadAll()
	if err != nil || len(list) == 0 {
		return
	}
	d, err := Dir()
	if err != nil {
		return
	}
	stamp := filepath.Join(d, "update-checked")
	if st, err := os.Stat(stamp); err == nil && time.Since(st.ModTime()) < 6*time.Hour {
		return
	}
	os.WriteFile(stamp, nil, 0o600)
	os.Chtimes(stamp, time.Now(), time.Now())
	l, err := update.Check(ctx, list[0].Server)
	if err != nil || !l.Auto || !update.Newer(l.Version, Version) || !update.Newer(l.Version, InstalledVersion()) {
		return
	}
	dst, err := installedPath()
	if err != nil {
		return
	}
	file, err := update.Fetch(ctx, list[0].Server, l, filepath.Dir(dst))
	if err == nil {
		err = update.Replace(dst, file)
	}
	if err != nil {
		logf("could not update to %s: %v", l.Version, err)
		return
	}
	setInstalledVersion(l.Version)
	logf("updated to %s; the next run uses it", l.Version)
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
	if self, err := os.Executable(); err == nil {
		update.Cleanup(self)
	}
	AutoUpdate(ctx, logf)
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

	// Every run reports whether Tally answered, so the bank sees "Tally closed"
	// rather than silence; with Tally closed there is nothing more to do.
	pctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	_, perr := tally.NewClient(c.TallyURL).Ping(pctx)
	cancel()
	beat := upload.Heartbeat{Tally: "ok", Version: version}
	if perr != nil {
		beat = upload.Heartbeat{Tally: "down", Error: perr.Error(), Version: version}
	}
	backend := upload.New(c.Server)
	st, err := backend.MonitorStatus(ctx, c.Token, beat)
	if perr != nil && err == nil && st.Active {
		return nil
	}
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
	if errors.Is(err, ErrTallyBusy) {
		logf("%s: %v; will try again on the next run", c.Company, err)
		return nil
	}
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
	unlockTally, err := LockTally(ctx, c.TallyURL, false, nil)
	if err != nil {
		return "", err
	}
	defer unlockTally()
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
	req := upload.StartRequest{
		Company: co, Period: extract.Period{From: from.Format("2006-01-02"), To: to.Format("2006-01-02")},
		TallyAlterID: co.AltVchID, ConnectorVersion: version, Machine: extract.MachineOf(o),
		Consent: extract.Consent{AcceptedBy: c.ConsentBy, AcceptedAt: c.ConsentAt, MonitoringOptIn: c.Daily,
			Text: fmt.Sprintf("Update under the consent given by %s on %s.", c.ConsentBy, c.ConsentAt)},
	}
	sess := &upload.Session{B: backend, Token: c.Token, SyncID: st.SyncID}
	summary, err := extract.Sync(ctx, tc, o, st.Plan, sess, progress)
	if err != nil || !sess.More {
		return summary, err
	}
	more, err := Continue(ctx, tc, o, backend, c.Token, req, sess, progress)
	return strings.TrimSpace(summary + " " + more), err
}

// Continue keeps a growing history going: while the server wants older months,
// it opens the next sync and sends them, each one also carrying what changed.
func Continue(ctx context.Context, tc *tally.Client, o extract.Options, b *upload.Backend, token string,
	req upload.StartRequest, sess *upload.Session, progress extract.Progress) (string, error) {
	var notes []string
	for round := 0; sess.More && round < 12; round++ {
		st, err := b.StartAgain(ctx, token, req)
		if err != nil {
			return strings.Join(notes, " "), err
		}
		sess = &upload.Session{B: b, Token: token, SyncID: st.SyncID}
		summary, err := extract.Sync(ctx, tc, o, st.Plan, sess, progress)
		if err != nil {
			return strings.Join(notes, " "), err
		}
		notes = append(notes, summary)
	}
	return strings.Join(notes, " "), nil
}

// ErrTallyBusy means another company on the same Tally is being synced.
var ErrTallyBusy = errors.New("another company on this Tally is being shared right now")

// LockTally lets one sync at a time talk to a Tally: Tally answers one request
// at a time, and two syncs at once (the page and a scheduled run, 2026-10-05)
// left each waiting on the other until Tally looked hung. With wait it blocks
// until the other sync finishes, calling onWait once; without, it returns
// ErrTallyBusy at once.
func LockTally(ctx context.Context, tallyURL string, wait bool, onWait func()) (func(), error) {
	d, err := Dir()
	if err != nil {
		return nil, err
	}
	sum := sha256.Sum256([]byte(strings.ToLower(strings.TrimRight(tallyURL, "/"))))
	p := filepath.Join(d, "tally-"+hex.EncodeToString(sum[:6])+".lock")
	for waited := false; ; waited = true {
		if release, ok := tryLock(p); ok {
			return release, nil
		}
		if !wait {
			return nil, ErrTallyBusy
		}
		if !waited && onWait != nil {
			onWait()
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(5 * time.Second):
		}
	}
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
		return nil, fmt.Errorf("%s is already being synced by another TallyVeda window", c.Company)
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

// ShipLogs sends the tail of connector.log to the server at once and then
// every 10 seconds while a sync runs, so support can see which Tally request
// was in flight even if Tally freezes and the connector is closed.
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
	awake := activity.KeepAwake()
	support.Reset()
	support.Send = func(kind, text string) {
		ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
		defer cancel()
		if err := b.SendLog(ctx, upload.TokenAuth(token), kind, text, session+"-"+kind); err != nil {
			log.Printf("could not send %s to the server: %v", kind, err)
		}
	}
	go support.Send("system", support.Report())
	stop := make(chan struct{})
	done := make(chan struct{})
	go func() {
		defer close(done)
		send()
		t := time.NewTicker(10 * time.Second)
		defer t.Stop()
		// Ticks come every 10 s; a much longer gap of wall-clock time means
		// this process wasn't running (the computer slept or froze).
		lastWall := time.Now().Round(0)
		for {
			select {
			case <-stop:
				send()
				return
			case <-t.C:
				wall := time.Now().Round(0)
				if gap := wall.Sub(lastWall); gap > 40*time.Second {
					log.Printf("this computer was asleep or frozen for about %s (no activity from %s to %s)",
						gap.Round(time.Second), lastWall.Format("15:04:05"), wall.Format("15:04:05"))
				}
				lastWall = wall
				send()
			}
		}
	}()
	return func() { close(stop); <-done; support.Send = nil; awake() }
}

// Logger returns a printf-style logger writing to w with timestamps.
func Logger(w io.Writer) func(string, ...any) {
	l := log.New(w, "", log.LstdFlags)
	return func(f string, a ...any) { l.Printf(f, a...) }
}

// FieldMemory remembers, in tally-skip.json, Tally fields that froze this
// computer's Tally, so later syncs skip them (see extract.FieldMemory).
type FieldMemory struct{}

func skipPath() string {
	d, _ := Dir()
	return filepath.Join(d, "tally-skip.json")
}

func (FieldMemory) list() []string {
	var out []string
	if raw, err := os.ReadFile(skipPath()); err == nil {
		json.Unmarshal(raw, &out)
	}
	return out
}

func (m FieldMemory) Skipped(field string) bool {
	for _, f := range m.list() {
		if f == field {
			return true
		}
	}
	return false
}

func (m FieldMemory) Froze(field string) {
	if m.Skipped(field) {
		return
	}
	raw, _ := json.Marshal(append(m.list(), field))
	os.WriteFile(skipPath(), raw, 0o600)
	log.Printf("remembering that Tally froze on field %s; it will be skipped from now on", field)
}
