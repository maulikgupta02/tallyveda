package monitor

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestPutReplacesAnEarlierAttemptForTheSameCompany(t *testing.T) {
	t.Setenv("TC_HOME", t.TempDir())
	old := &Config{Server: "s", Token: "t1", Company: "Acme", BankName: "Demo Bank", Pending: true}
	other := &Config{Server: "s", Token: "t2", Company: "Acme", BankName: "Other Bank", Pending: true}
	for _, c := range []*Config{old, other, {Server: "s", Token: "t3", Company: "Acme", BankName: "Demo Bank"}} {
		if err := Put(c); err != nil {
			t.Fatal(err)
		}
	}
	list, _ := LoadAll()
	if len(list) != 2 || list[0].Token != "t2" || list[1].Token != "t3" {
		t.Fatalf("want the other requester's entry and the new attempt, got %+v", list)
	}
}

func TestPruneDropsRevokedEntriesAndKeepsTheNewestDuplicate(t *testing.T) {
	t.Setenv("TC_HOME", t.TempDir())
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.Header.Get("Authorization"), "deleted") {
			http.Error(w, `{"detail":"Unknown token"}`, http.StatusUnauthorized)
			return
		}
		w.Write([]byte(`{"active": true}`))
	}))
	defer srv.Close()
	// Written directly, as older connectors did, so the duplicates exist.
	if err := saveAll([]*Config{
		{Server: srv.URL, Token: "a-old", Company: "Acme", BankName: "B", InstalledAt: "2026-10-03T10:00:00Z"},
		{Server: srv.URL, Token: "a-new", Company: "Acme", BankName: "B", InstalledAt: "2026-10-04T10:00:00Z"},
		{Server: srv.URL, Token: "x-deleted", Company: "Gone Co", BankName: "B", InstalledAt: "2026-10-04T11:00:00Z"},
	}); err != nil {
		t.Fatal(err)
	}
	if err := Prune(context.Background()); err != nil {
		t.Fatal(err)
	}
	list, _ := LoadAll()
	if len(list) != 1 || list[0].Token != "a-new" {
		t.Fatalf("want only the newest Acme entry, got %+v", list)
	}
}

func TestLockTallyOneSyncAtATime(t *testing.T) {
	t.Setenv("TC_HOME", t.TempDir())
	ctx := context.Background()
	release, err := LockTally(ctx, "http://localhost:9000", false, nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := LockTally(ctx, "http://LOCALHOST:9000/", false, nil); !errors.Is(err, ErrTallyBusy) {
		t.Fatalf("second sync on the same Tally: got %v, want ErrTallyBusy", err)
	}
	other, err := LockTally(ctx, "http://localhost:9001", false, nil)
	if err != nil {
		t.Fatalf("a different Tally should not be blocked: %v", err)
	}
	other()
	release()
	again, err := LockTally(ctx, "http://localhost:9000", false, nil)
	if err != nil {
		t.Fatalf("after release: %v", err)
	}
	again()
}
