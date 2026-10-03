package tally

import (
	"context"
	"fmt"
	"html"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// collectionRequest builds an inline-TDL collection export. Collections return
// computed fields (ClosingBalance as at SVTODATE) that master exports don't.
// A non-empty childOf limits it to the direct children of that group or stock
// group, which keeps each request small enough for Tally to answer quickly.
func collectionRequest(company, objType, childOf string, fetch []string, from, to time.Time) string {
	var sv strings.Builder
	sv.WriteString("<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>")
	if company != "" {
		fmt.Fprintf(&sv, "<SVCURRENTCOMPANY>%s</SVCURRENTCOMPANY>", html.EscapeString(company))
	}
	if !from.IsZero() {
		fmt.Fprintf(&sv, "<SVFROMDATE>%s</SVFROMDATE>", tallyDate(from))
	}
	if !to.IsZero() {
		fmt.Fprintf(&sv, "<SVTODATE>%s</SVTODATE>", tallyDate(to))
	}
	var child string
	if childOf != "" {
		child = "<CHILDOF>" + html.EscapeString(childOf) + "</CHILDOF>"
	}
	return fmt.Sprintf(`<ENVELOPE>
<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>TCColl</ID></HEADER>
<BODY><DESC><STATICVARIABLES>%s</STATICVARIABLES>
<TDL><TDLMESSAGE><COLLECTION NAME="TCColl" ISMODIFY="No"><TYPE>%s</TYPE>%s<FETCH>%s</FETCH></COLLECTION></TDLMESSAGE></TDL>
</DESC></BODY></ENVELOPE>`, sv.String(), objType, child, strings.Join(fetch, ", "))
}

// dayBookRequest exports full vouchers for a date range. The Day Book report
// is present in every Tally version and returns every voucher field.
func dayBookRequest(company string, from, to time.Time) string {
	return fmt.Sprintf(`<ENVELOPE>
<HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
<BODY><EXPORTDATA><REQUESTDESC><REPORTNAME>Day Book</REPORTNAME>
<STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT><SVCURRENTCOMPANY>%s</SVCURRENTCOMPANY>
<SVFROMDATE>%s</SVFROMDATE><SVTODATE>%s</SVTODATE></STATICVARIABLES>
</REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>`, html.EscapeString(company), tallyDate(from), tallyDate(to))
}

func tallyDate(t time.Time) string { return t.Format("20060102") }

// ------------------------------------------------------------------ objects

type Company struct {
	Name      string `json:"name"`
	GUID      string `json:"guid,omitempty"`
	BooksFrom string `json:"books_from,omitempty"`
	StartFrom string `json:"starting_from,omitempty"`
	State     string `json:"state,omitempty"`
	PAN       string `json:"pan,omitempty"`
}

func (c *Client) Companies(ctx context.Context) ([]Company, error) {
	root, err := c.Post(ctx, collectionRequest("", "Company", "",
		[]string{"Name", "GUID", "StartingFrom", "BooksFrom", "StateName", "IncomeTaxNumber"}, time.Time{}, time.Time{}))
	if err != nil {
		return nil, err
	}
	var out []Company
	for _, n := range root.FindAll("COMPANY") {
		name := n.ObjectName()
		if name == "" {
			continue
		}
		out = append(out, Company{
			Name:      name,
			GUID:      n.Field("GUID"),
			BooksFrom: isoDate(n.Field("BOOKSFROM")),
			StartFrom: isoDate(n.Field("STARTINGFROM")),
			State:     n.Field("STATENAME"),
			PAN:       n.Field("INCOMETAXNUMBER"),
		})
	}
	return out, nil
}

// Collection fetches objects of one type and returns their nodes.
func (c *Client) Collection(ctx context.Context, company, objType string, fetch []string, from, to time.Time) ([]*Node, error) {
	return c.CollectionOf(ctx, company, objType, "", fetch, from, to)
}

// CollectionOf is Collection limited to the direct children of parent ("" for all).
func (c *Client) CollectionOf(ctx context.Context, company, objType, parent string, fetch []string, from, to time.Time) ([]*Node, error) {
	root, err := c.Post(ctx, collectionRequest(company, objType, parent, fetch, from, to))
	if err != nil {
		return nil, err
	}
	tag := strings.ToUpper(objType)
	nodes := root.FindAll(tag)
	if len(nodes) == 0 && strings.HasSuffix(tag, "S") {
		nodes = root.FindAll(strings.TrimSuffix(tag, "S")) // "Bills" collection yields <BILL> objects
	}
	return nodes, nil
}

// Vouchers returns the VOUCHER nodes of the Day Book for [from, to].
func (c *Client) Vouchers(ctx context.Context, company string, from, to time.Time) ([]*Node, error) {
	root, err := c.Post(ctx, dayBookRequest(company, from, to))
	if err != nil {
		return nil, err
	}
	return root.FindAll("VOUCHER"), nil
}

// ------------------------------------------------------------------ values

var numRe = regexp.MustCompile(`-?[0-9][0-9,]*(\.[0-9]+)?`)

// Amount parses a Tally amount. Tally writes debits as negative numbers;
// report-style values may instead carry a "Dr"/"Cr" suffix. Foreign-currency
// amounts look like "-$ 100.00 @ ₹ 82.00/$ = -₹ 8200.00"; the base-currency
// value after "=" is used.
func Amount(s string) float64 {
	s = strings.TrimSpace(s)
	if s == "" {
		return 0
	}
	if i := strings.LastIndex(s, "="); i >= 0 {
		s = s[i+1:]
	}
	neg := strings.Contains(s, "-")
	m := numRe.FindString(strings.ReplaceAll(s, " ", ""))
	if m == "" {
		return 0
	}
	v, err := strconv.ParseFloat(strings.ReplaceAll(strings.TrimPrefix(m, "-"), ",", ""), 64)
	if err != nil {
		return 0
	}
	lower := strings.ToLower(s)
	switch {
	case strings.HasSuffix(lower, "dr"):
		return -v
	case strings.HasSuffix(lower, "cr"):
		return v
	case neg:
		return -v
	}
	return v
}

// DebitPositive converts a Tally amount (debit negative) to the bundle's
// debit-positive convention.
func DebitPositive(s string) float64 {
	v := -Amount(s)
	if v == 0 {
		return 0 // avoid -0 in JSON
	}
	return v
}

func Bool(s string) bool { return strings.EqualFold(strings.TrimSpace(s), "yes") }

func Int(s string) int64 {
	v, _ := strconv.ParseInt(strings.TrimSpace(s), 10, 64)
	return v
}

var dateLayouts = []string{"20060102", "2-Jan-2006", "2-Jan-06", "02-01-2006", "2006-01-02", "2 Jan 2006"}

func ParseDate(s string) (time.Time, bool) {
	s = strings.TrimSpace(s)
	for _, l := range dateLayouts {
		if t, err := time.Parse(l, s); err == nil {
			return t, true
		}
	}
	return time.Time{}, false
}

func isoDate(s string) string {
	if t, ok := ParseDate(s); ok {
		return t.Format("2006-01-02")
	}
	return ""
}

// ISODate formats a Tally date field as YYYY-MM-DD ("" if unparseable).
func ISODate(s string) string { return isoDate(s) }

var daysRe = regexp.MustCompile(`(\d+)\s*Days?`)

// CreditDays parses a credit period such as "30 Days".
func CreditDays(s string) *int {
	if m := daysRe.FindStringSubmatch(s); m != nil {
		v, _ := strconv.Atoi(m[1])
		return &v
	}
	return nil
}
