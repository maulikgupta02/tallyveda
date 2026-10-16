// Package telemetry sends anonymous technical events to the server from the
// moment the connector starts, before any access code is entered, so support
// can see why Tally isn't found without asking the MSME. Events carry no
// company names or accounting data, and sending never blocks or fails the
// connector.
package telemetry

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"
)

var (
	mu      sync.Mutex
	server  string
	version string
	install string
	last    = map[string]sent{}
	client  = &http.Client{Timeout: 90 * time.Second} // covers the server's cold start
	pending sync.WaitGroup
)

type sent struct {
	body string
	at   time.Time
}

// Init sets where events go. An empty server turns telemetry off.
func Init(serverURL, connectorVersion string) {
	mu.Lock()
	defer mu.Unlock()
	server, version = strings.TrimRight(serverURL, "/"), connectorVersion
	install = installID()
}

// Install is this computer's random telemetry id, for matching events to a
// support case.
func Install() string {
	mu.Lock()
	defer mu.Unlock()
	return install
}

// Send queues one event: start, tally, probe or error. The same event with
// the same body is sent at most every 10 minutes.
func Send(event, body string) {
	body = Redact(body)
	mu.Lock()
	if server == "" || install == "" {
		mu.Unlock()
		return
	}
	if p, ok := last[event]; ok && p.body == body && time.Since(p.at) < 10*time.Minute {
		mu.Unlock()
		return
	}
	last[event] = sent{body, time.Now()}
	payload, _ := json.Marshal(map[string]string{"install_id": install, "version": version, "event": event, "body": body})
	url := server + "/api/connector/telemetry"
	mu.Unlock()
	pending.Add(1)
	go func() {
		defer pending.Done()
		resp, err := client.Post(url, "application/json", bytes.NewReader(payload))
		if err == nil {
			resp.Body.Close()
		}
	}()
}

// Flush waits up to max for queued events, e.g. before the process exits.
func Flush(max time.Duration) {
	done := make(chan struct{})
	go func() { pending.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(max):
	}
}

var userDir = regexp.MustCompile(`(?i)(\\Users\\|/Users/|/home/)[^\\/\s;,)"]+`)

// Redact hides the Windows user name in paths.
func Redact(s string) string { return userDir.ReplaceAllString(s, "${1}…") }

func installID() string {
	dir := os.Getenv("TC_HOME")
	if dir == "" {
		base, err := os.UserConfigDir()
		if err != nil {
			return ""
		}
		dir = filepath.Join(base, "TallyConnector")
	}
	path := filepath.Join(dir, "install-id")
	if b, err := os.ReadFile(path); err == nil {
		if id := strings.TrimSpace(string(b)); len(id) == 32 {
			return id
		}
	}
	buf := make([]byte, 16)
	if _, err := rand.Read(buf); err != nil {
		return ""
	}
	id := hex.EncodeToString(buf)
	if os.MkdirAll(dir, 0o700) == nil {
		os.WriteFile(path, []byte(id), 0o600)
	}
	return id
}
