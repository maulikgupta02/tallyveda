package tally

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestAmount(t *testing.T) {
	cases := map[string]float64{
		"-11800.00":                          -11800,
		"1,23,456.50":                        123456.5,
		"5000.00 Dr":                         -5000,
		"5000.00 Cr":                         5000,
		"-$ 100.00 @ ₹ 82.00/$ = -₹ 8200.00": -8200,
		"":                                   0,
	}
	for in, want := range cases {
		if got := Amount(in); got != want {
			t.Errorf("Amount(%q) = %v, want %v", in, got, want)
		}
	}
	if DebitPositive("-100.00") != 100 {
		t.Error("debit should become positive")
	}
}

func TestSanitizeAndParse(t *testing.T) {
	raw := "<ENVELOPE><GROUP NAME=\"Current Assets\"><PARENT>&#4; Primary</PARENT></GROUP>\x04</ENVELOPE>"
	root, err := Parse(sanitize(raw))
	if err != nil {
		t.Fatal(err)
	}
	g := root.FindAll("GROUP")
	if len(g) != 1 || g[0].ObjectName() != "Current Assets" || g[0].Field("PARENT") != "Primary" {
		t.Fatalf("unexpected parse: %+v", g)
	}
}

func TestDecodeUTF16(t *testing.T) {
	s := "<A>₹ Kolkata</A>"
	if got := decode(encodeUTF16LE(s)); got != s {
		t.Fatalf("with BOM: %q", got)
	}
	if got := decode(encodeUTF16LE(s)[2:]); got != s {
		t.Fatalf("without BOM: %q", got)
	}
}

func TestResponseError(t *testing.T) {
	root, _ := Parse("<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>0</STATUS></HEADER><BODY><DATA><LINEERROR>Could not set company</LINEERROR></DATA></BODY></ENVELOPE>")
	if responseError(root) != "Could not set company" {
		t.Fatal("expected line error")
	}
}

func TestParseDate(t *testing.T) {
	for _, s := range []string{"20240401", "1-Apr-2024", "1-Apr-24"} {
		d, ok := ParseDate(s)
		if !ok || d.Format("2006-01-02") != "2024-04-01" {
			t.Errorf("ParseDate(%q) = %v %v", s, d, ok)
		}
	}
}

// A relay that mangles UTF-16 makes Tally list no companies; Companies must
// retry in UTF-8 and keep using it.
func TestCompaniesFallsBackToUTF8(t *testing.T) {
	var utf16Seen, utf8Seen int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.Header.Get("Content-Type"), "utf-16") {
			utf16Seen++
			io.WriteString(w, "<ENVELOPE></ENVELOPE>")
			return
		}
		utf8Seen++
		io.WriteString(w, `<ENVELOPE><COMPANY NAME="Acme Traders"><GUID>g1</GUID></COMPANY></ENVELOPE>`)
	}))
	defer srv.Close()
	c := NewClient(srv.URL)
	cos, err := c.Companies(context.Background())
	if err != nil || len(cos) != 1 || cos[0].Name != "Acme Traders" {
		t.Fatalf("got %+v, %v", cos, err)
	}
	if c.UsesUTF16() {
		t.Fatal("client should have switched to UTF-8")
	}
	if _, err := c.Companies(context.Background()); err != nil || utf16Seen != 1 || utf8Seen != 2 {
		t.Fatalf("second call should go straight to UTF-8: utf16=%d utf8=%d err=%v", utf16Seen, utf8Seen, err)
	}
}

// With no company loaded in either encoding, the client keeps UTF-16.
func TestCompaniesKeepsEncodingWhenNoneOpen(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, "<ENVELOPE></ENVELOPE>")
	}))
	defer srv.Close()
	c := NewClient(srv.URL)
	if cos, err := c.Companies(context.Background()); err != nil || len(cos) != 0 {
		t.Fatalf("got %+v, %v", cos, err)
	}
	if !c.UsesUTF16() {
		t.Fatal("client should stay on UTF-16")
	}
}
