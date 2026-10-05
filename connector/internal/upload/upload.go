// Package upload talks to the bank backend: code verification, bundle upload
// and the daily-monitoring endpoints.
package upload

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

type Info struct {
	BankName          string `json:"bank_name"`
	ApplicantName     string `json:"applicant_name"`
	Reference         string `json:"reference"`
	Months            int    `json:"months"`
	MonitoringOffered bool   `json:"monitoring_offered"`
	OTPRequired       bool   `json:"otp_required"`
	OTPSentTo         string `json:"otp_sent_to"`
	ContactMissing    bool   `json:"contact_missing"`
}

type UploadResult struct {
	BankName     string `json:"bank_name"`
	MonitorToken string `json:"monitor_token"`
}

type MonitorStatus struct {
	Active        bool   `json:"active"`
	Status        string `json:"status"`
	Due           bool   `json:"due"`
	BankName      string `json:"bank_name"`
	ApplicantName string `json:"applicant_name"`
	Months        int    `json:"months"`
	LastReportAt  string `json:"last_report_at"`
	Resume        bool   `json:"resume"` // a sync was interrupted and should continue
}

// ErrTokenRevoked means the bank no longer recognises the monitoring token.
var ErrTokenRevoked = errors.New("monitoring token not recognised by the bank")

type Backend struct {
	URL  string
	HTTP *http.Client
}

func New(url string) *Backend {
	// 15 minutes comfortably covers a free-tier backend's cold start (sleeps after
	// 15 min idle, ~1 minute to wake) plus a large upload's own processing time.
	return &Backend{URL: strings.TrimRight(url, "/"), HTTP: &http.Client{Timeout: 15 * time.Minute}}
}

// Verify checks a one-time code and returns who is requesting the data.
func (b *Backend) Verify(ctx context.Context, code string) (*Info, error) {
	var info Info
	body, _ := json.Marshal(map[string]string{"code": code})
	return &info, b.call(ctx, "/api/connector/verify", nil, body, &info)
}

// Upload sends the first bundle, authorised by the one-time code.
func (b *Backend) Upload(ctx context.Context, code string, bundle any) (*UploadResult, error) {
	var res UploadResult
	return &res, b.upload(ctx, "/api/connector/upload", map[string]string{"X-Link-Code": code}, bundle, &res)
}

// MonitorUpload sends a daily refresh, authorised by the monitoring token.
func (b *Backend) MonitorUpload(ctx context.Context, token string, bundle any) error {
	return b.upload(ctx, "/api/connector/monitor/upload", bearer(token), bundle, nil)
}

// Heartbeat is what a scheduled run found on this computer, so the bank can
// tell "Tally is closed" from "the computer is off". Tally is "ok" or "down";
// empty when the caller didn't check.
type Heartbeat struct {
	Tally   string `json:"tally,omitempty"`
	Error   string `json:"error,omitempty"`
	Version string `json:"version,omitempty"`
}

func (b *Backend) MonitorStatus(ctx context.Context, token string, beat Heartbeat) (*MonitorStatus, error) {
	var st MonitorStatus
	body, _ := json.Marshal(beat)
	return &st, b.call(ctx, "/api/connector/monitor/status", bearer(token), body, &st)
}

func (b *Backend) MonitorStop(ctx context.Context, token string) error {
	return b.call(ctx, "/api/connector/monitor/stop", bearer(token), []byte("{}"), nil)
}

func bearer(token string) map[string]string {
	return map[string]string{"Authorization": "Bearer " + token}
}

// call makes one small JSON request (verify/monitor status/monitor stop),
// retrying on a connection failure — the bank's backend may be a free-tier
// instance asleep after 15 minutes idle, so the very first request after a
// gap can hit "connection refused" while it wakes up, not just be slow.
func (b *Backend) call(ctx context.Context, path string, headers map[string]string, body []byte, out any) error {
	var lastErr error
	for attempt := 0; attempt < 4; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(time.Duration(attempt*attempt) * 3 * time.Second):
			}
		}
		req, _ := http.NewRequestWithContext(ctx, http.MethodPost, b.URL+path, bytes.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		for k, v := range headers {
			req.Header.Set(k, v)
		}
		resp, err := b.HTTP.Do(req)
		if err != nil {
			lastErr = fmt.Errorf("could not reach the bank's server (%s). Check your internet connection: %v", b.URL, err)
			continue
		}
		if resp.StatusCode != http.StatusOK {
			lastErr = apiError(resp)
			resp.Body.Close()
			if resp.StatusCode < 500 {
				return lastErr // not retryable (bad code, unauthorised…)
			}
			continue
		}
		defer resp.Body.Close()
		if out == nil {
			return nil
		}
		return json.NewDecoder(resp.Body).Decode(out)
	}
	return lastErr
}

// upload gzips the bundle and posts it, retrying transient failures.
func (b *Backend) upload(ctx context.Context, path string, headers map[string]string, bundle any, out any) error {
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	if err := json.NewEncoder(gz).Encode(bundle); err != nil {
		return err
	}
	if err := gz.Close(); err != nil {
		return err
	}
	var lastErr error
	for attempt := 0; attempt < 4; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(time.Duration(attempt*attempt) * 3 * time.Second):
			}
		}
		req, _ := http.NewRequestWithContext(ctx, http.MethodPost, b.URL+path, bytes.NewReader(buf.Bytes()))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Content-Encoding", "gzip")
		for k, v := range headers {
			req.Header.Set(k, v)
		}
		resp, err := b.HTTP.Do(req)
		if err != nil {
			lastErr = fmt.Errorf("upload failed: %v", err)
			continue
		}
		if resp.StatusCode == http.StatusOK {
			defer resp.Body.Close()
			if out != nil {
				return json.NewDecoder(resp.Body).Decode(out)
			}
			return nil
		}
		lastErr = apiError(resp)
		resp.Body.Close()
		if resp.StatusCode < 500 {
			return lastErr // not retryable (bad code, too large…)
		}
	}
	return lastErr
}

func apiError(resp *http.Response) error {
	if resp.StatusCode == http.StatusUnauthorized {
		return ErrTokenRevoked
	}
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 8192))
	var e struct {
		Detail any `json:"detail"`
	}
	if json.Unmarshal(raw, &e) == nil && e.Detail != nil {
		return fmt.Errorf("%v", e.Detail)
	}
	return fmt.Errorf("bank server returned HTTP %d", resp.StatusCode)
}
