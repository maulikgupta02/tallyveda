package telemetry

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestSendRedactsDedupesAndKeepsInstallID(t *testing.T) {
	var mu sync.Mutex
	var got []map[string]string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var m map[string]string
		json.NewDecoder(r.Body).Decode(&m)
		mu.Lock()
		got = append(got, m)
		mu.Unlock()
	}))
	defer srv.Close()
	t.Setenv("TC_HOME", t.TempDir())

	Init(srv.URL, "9.9.9")
	id := Install()
	if len(id) != 32 {
		t.Fatalf("install id %q", id)
	}
	Send("tally", `Port 9000 is held by tally.exe (C:\Users\Ramesh Kumar\TallyPrime\tally.exe, pid 1)`)
	Send("tally", `Port 9000 is held by tally.exe (C:\Users\Ramesh Kumar\TallyPrime\tally.exe, pid 1)`)
	Send("start", "hello")
	Flush(5 * time.Second)

	mu.Lock()
	defer mu.Unlock()
	if len(got) != 2 {
		t.Fatalf("want 2 events (duplicate dropped), got %d: %v", len(got), got)
	}
	for _, e := range got {
		if e["install_id"] != id || e["version"] != "9.9.9" {
			t.Fatalf("bad event %v", e)
		}
		if strings.Contains(e["body"], "Ramesh") {
			t.Fatalf("user name not redacted: %q", e["body"])
		}
	}
	Init(srv.URL, "9.9.9")
	if Install() != id {
		t.Fatal("install id should persist across runs")
	}
}
