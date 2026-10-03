// Package tally talks to TallyPrime / Tally.ERP 9 over its built-in XML/HTTP
// server (default port 9000). Nothing is installed inside Tally: the user only
// has to enable the server (F1 > Settings > Connectivity in TallyPrime).
package tally

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strings"
	"time"
	"unicode/utf16"
	"unicode/utf8"
)

type Client struct {
	URL  string
	HTTP *http.Client
	// UTF16 sends requests as UTF-16LE, which Tally needs to return
	// non-ASCII names (Hindi, ₹) intact. Responses are auto-detected.
	UTF16 bool
}

func NewClient(url string) *Client {
	return &Client{
		URL:   strings.TrimRight(url, "/"),
		HTTP:  &http.Client{Timeout: 10 * time.Minute},
		UTF16: true,
	}
}

var ErrNotRunning = errors.New("tally is not reachable")

// Ping checks that Tally's HTTP server answers. It returns the banner text,
// e.g. "TallyPrime Server is Running".
func (c *Client) Ping(ctx context.Context) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, c.URL, nil)
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return "", fmt.Errorf("%w at %s: %v", ErrNotRunning, c.URL, err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	text := strings.TrimSpace(stripTags(decode(body)))
	if !strings.Contains(strings.ToLower(text), "running") {
		return text, fmt.Errorf("%w: unexpected response from %s: %q", ErrNotRunning, c.URL, text)
	}
	return text, nil
}

// WaitIdle blocks until Tally answers a ping again. Tally serves requests one
// at a time on the same thread as its own screen, so an answer means it has
// finished whatever it was working on, including a request we gave up on.
func (c *Client) WaitIdle(ctx context.Context, max time.Duration) error {
	deadline := time.Now().Add(max)
	for {
		if _, err := c.Ping(ctx); err == nil {
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("Tally was still busy after %s", max.Round(time.Minute))
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(3 * time.Second):
		}
	}
}

// Post sends an XML request envelope and returns the parsed response tree.
func (c *Client) Post(ctx context.Context, envelope string) (*Node, error) {
	var body []byte
	contentType := "text/xml;charset=utf-8"
	if c.UTF16 {
		body = encodeUTF16LE(envelope)
		contentType = "text/xml;charset=utf-16"
	} else {
		body = []byte(envelope)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.URL, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", contentType)
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrNotRunning, err)
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("reading tally response: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("tally returned HTTP %d", resp.StatusCode)
	}
	root, err := Parse(sanitize(decode(raw)))
	if err != nil {
		return nil, fmt.Errorf("parsing tally response: %w", err)
	}
	if msg := responseError(root); msg != "" {
		return nil, &RequestError{Message: msg}
	}
	return root, nil
}

// RequestError is an error Tally reported (bad company name, unknown report…).
type RequestError struct{ Message string }

func (e *RequestError) Error() string { return "tally: " + e.Message }

func responseError(root *Node) string {
	if n := root.Find("LINEERROR"); n != nil {
		return n.Text
	}
	if h := root.Child("HEADER"); h != nil && h.ChildText("STATUS") == "0" {
		if d := root.Find("DATA"); d != nil && strings.TrimSpace(d.Text) != "" {
			return strings.TrimSpace(d.Text)
		}
		return "request failed"
	}
	if root.Name == "RESPONSE" && strings.Contains(strings.ToLower(root.Text), "error") {
		return root.Text
	}
	return ""
}

func encodeUTF16LE(s string) []byte {
	u := utf16.Encode([]rune(s))
	b := make([]byte, 0, len(u)*2+2)
	b = append(b, 0xFF, 0xFE)
	for _, r := range u {
		b = append(b, byte(r), byte(r>>8))
	}
	return b
}

// decode returns the response as UTF-8, detecting UTF-16 by BOM or by the
// NUL bytes an ASCII-heavy UTF-16 payload always contains.
func decode(b []byte) string {
	le := len(b) >= 2 && b[0] == 0xFF && b[1] == 0xFE
	be := len(b) >= 2 && b[0] == 0xFE && b[1] == 0xFF
	if le || be {
		b = b[2:]
	} else if len(b) >= 4 && b[1] == 0 && b[3] == 0 {
		le = true
	} else if len(b) >= 4 && b[0] == 0 && b[2] == 0 {
		be = true
	}
	if !le && !be {
		if utf8.Valid(b) {
			return string(b)
		}
		return latin1(b)
	}
	u := make([]uint16, len(b)/2)
	for i := range u {
		if le {
			u[i] = uint16(b[2*i]) | uint16(b[2*i+1])<<8
		} else {
			u[i] = uint16(b[2*i])<<8 | uint16(b[2*i+1])
		}
	}
	return string(utf16.Decode(u))
}

func latin1(b []byte) string {
	r := make([]rune, len(b))
	for i, c := range b {
		r[i] = rune(c)
	}
	return string(r)
}

var (
	badCharRef = regexp.MustCompile(`&#(x[0-9A-Fa-f]+|[0-9]+);`)
	tagRe      = regexp.MustCompile(`<[^>]*>`)
)

// sanitize removes characters that are illegal in XML 1.0. Tally emits them
// routinely, e.g. "&#4; Primary" as the parent of top-level groups.
func sanitize(s string) string {
	s = badCharRef.ReplaceAllStringFunc(s, func(ref string) string {
		var n int
		if ref[2] == 'x' || ref[2] == 'X' {
			fmt.Sscanf(ref[3:len(ref)-1], "%x", &n)
		} else {
			fmt.Sscanf(ref[2:len(ref)-1], "%d", &n)
		}
		if n == 9 || n == 10 || n == 13 || n >= 32 {
			return ref
		}
		return ""
	})
	return strings.Map(func(r rune) rune {
		if r == '\t' || r == '\n' || r == '\r' || r >= 32 {
			if r == 0xFFFE || r == 0xFFFF {
				return -1
			}
			return r
		}
		return -1
	}, s)
}

func stripTags(s string) string { return tagRe.ReplaceAllString(s, "") }
