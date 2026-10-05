package update

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

func TestNewer(t *testing.T) {
	cases := []struct {
		a, b string
		want bool
	}{{"0.5.0", "0.4.12", true}, {"0.4.12", "0.5.0", false}, {"0.4.2", "0.4.2", false}, {"1.0.0", "0.9.9", true},
		{"0.5.0", "dev", false}, {"dev", "0.1.0", false}, {"0.5.0", "", false}}
	for _, c := range cases {
		if got := Newer(c.a, c.b); got != c.want {
			t.Errorf("Newer(%q, %q) = %v", c.a, c.b, got)
		}
	}
}

func TestFetchVerifiesAndReplaceSwaps(t *testing.T) {
	body := []byte("new connector build")
	sum := sha256.Sum256(body)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/connector/latest":
			fmt.Fprintf(w, `{"version":"0.5.0","sha256":"%s","size":%d,"url":"/download/connector.exe","auto":true}`,
				hex.EncodeToString(sum[:]), len(body))
		case "/download/connector.exe":
			w.Write(body)
		}
	}))
	defer srv.Close()
	dir := t.TempDir()
	target := filepath.Join(dir, "TallyConnector.exe")
	os.WriteFile(target, []byte("old"), 0o755)

	l, err := Check(context.Background(), srv.URL)
	if err != nil || !l.Auto || l.Version != "0.5.0" {
		t.Fatalf("check: %+v %v", l, err)
	}
	file, err := Fetch(context.Background(), srv.URL, l, dir)
	if err != nil {
		t.Fatal(err)
	}
	if err := Replace(target, file); err != nil {
		t.Fatal(err)
	}
	if got, _ := os.ReadFile(target); string(got) != string(body) {
		t.Fatalf("target holds %q", got)
	}
	if old, _ := os.ReadFile(target + ".old"); string(old) != "old" {
		t.Fatalf("old copy holds %q", old)
	}

	bad := *l
	bad.SHA256 = hex.EncodeToString(make([]byte, 32))
	if _, err := Fetch(context.Background(), srv.URL, &bad, dir); err == nil {
		t.Fatal("a download that doesn't match the fingerprint must be rejected")
	}
	if left, _ := filepath.Glob(filepath.Join(dir, "*.download")); len(left) != 0 {
		t.Fatalf("rejected download left behind: %v", left)
	}
}
