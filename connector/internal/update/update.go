// Package update keeps the connector current: it asks the server for the
// newest release, downloads it over HTTPS, checks its SHA-256 and size against
// what the server announced, and swaps it in place of an exe on disk.
package update

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// Latest is the server's announcement of the newest connector.
type Latest struct {
	Version string `json:"version"`
	SHA256  string `json:"sha256"`
	Size    int64  `json:"size"`
	URL     string `json:"url"`
	// Auto: background runs may install it without asking. The server can turn
	// this off without a new release.
	Auto bool `json:"auto"`
}

var client = &http.Client{Timeout: 10 * time.Minute}

// Check asks server for the newest release.
func Check(ctx context.Context, server string) (*Latest, error) {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(server, "/")+"/api/connector/latest", nil)
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("update check returned HTTP %d", resp.StatusCode)
	}
	var l Latest
	if err := json.NewDecoder(resp.Body).Decode(&l); err != nil {
		return nil, err
	}
	if l.Version == "" || len(l.SHA256) != 64 || l.Size <= 0 || l.URL == "" {
		return nil, errors.New("the server's update information is incomplete")
	}
	return &l, nil
}

// Newer reports whether version a is newer than b ("0.5.0" > "0.4.12").
// A development build ("dev") is never updated and never offered.
func Newer(a, b string) bool {
	pa, oka := parse(a)
	pb, okb := parse(b)
	if !oka || !okb {
		return false
	}
	for i := range pa {
		if pa[i] != pb[i] {
			return pa[i] > pb[i]
		}
	}
	return false
}

func parse(v string) ([3]int, bool) {
	var out [3]int
	parts := strings.Split(strings.TrimPrefix(strings.TrimSpace(v), "v"), ".")
	if len(parts) != 3 {
		return out, false
	}
	for i, p := range parts {
		n, err := strconv.Atoi(p)
		if err != nil {
			return out, false
		}
		out[i] = n
	}
	return out, true
}

// Fetch downloads the release next to dir and verifies it. It returns the
// path of the verified file, which the caller installs with Replace.
func Fetch(ctx context.Context, server string, l *Latest, dir string) (string, error) {
	url := l.URL
	if strings.HasPrefix(url, "/") {
		url = strings.TrimRight(server, "/") + url
	}
	if !strings.HasPrefix(url, "https://") && !strings.HasPrefix(url, "http://127.0.0.1") && !strings.HasPrefix(url, "http://localhost") {
		return "", errors.New("refusing to download an update over an insecure connection")
	}
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("download returned HTTP %d", resp.StatusCode)
	}
	f, err := os.CreateTemp(dir, "TallyConnector-*.download")
	if err != nil {
		return "", err
	}
	h := sha256.New()
	n, err := io.Copy(io.MultiWriter(f, h), io.LimitReader(resp.Body, l.Size+1))
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	if err == nil && n != l.Size {
		err = fmt.Errorf("the download was %d bytes, expected %d", n, l.Size)
	}
	if err == nil && !strings.EqualFold(hex.EncodeToString(h.Sum(nil)), l.SHA256) {
		err = errors.New("the download did not match the published fingerprint")
	}
	if err != nil {
		os.Remove(f.Name())
		return "", err
	}
	os.Chmod(f.Name(), 0o755)
	return f.Name(), nil
}

// Replace puts the verified file at target. A running exe can't be
// overwritten on Windows but can be renamed, so the old one moves aside to
// target.old (removed by Cleanup on a later start).
func Replace(target, verified string) error {
	old := target + ".old"
	os.Remove(old)
	if _, err := os.Stat(target); err == nil {
		if err := os.Rename(target, old); err != nil {
			return fmt.Errorf("moving the old version aside: %w", err)
		}
	}
	if err := os.Rename(verified, target); err != nil {
		os.Rename(old, target)
		return fmt.Errorf("installing the new version: %w", err)
	}
	return nil
}

// Cleanup removes what earlier updates left behind next to exe.
func Cleanup(exe string) {
	os.Remove(exe + ".old")
	if matches, _ := filepath.Glob(filepath.Join(filepath.Dir(exe), "TallyConnector-*.download")); len(matches) > 0 {
		for _, m := range matches {
			if st, err := os.Stat(m); err == nil && time.Since(st.ModTime()) > time.Hour {
				os.Remove(m)
			}
		}
	}
}
