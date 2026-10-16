package diagnose

import (
	"net/url"
	"strconv"
	"strings"

	"tallyconnector/internal/support"
	"tallyconnector/internal/tally"
)

// Explain describes what is answering at Tally's address, for when no
// company can be found: which program holds the port, whether a Tally runs on
// this computer itself, and what Tally actually replied.
func Explain(c *tally.Client) []string {
	var out []string
	port := 9000
	if u, err := url.Parse(c.Address()); err == nil {
		if p, err := strconv.Atoi(u.Port()); err == nil {
			port = p
		}
	}
	if owner := support.PortOwner(port); owner != "" {
		out = append(out, "Port "+strconv.Itoa(port)+" is held by "+owner)
	}
	if local := support.LocalTally(); len(local) > 0 {
		out = append(out, "Tally running on this computer: "+strings.Join(local, "; "))
	}
	if n := c.Note(); n != "" {
		out = append(out, "Company list: "+n)
	}
	if r := c.LastReply(); r != "" {
		out = append(out, "Tally replied: "+r)
	}
	return out
}

// ExplainSafe is Explain with Tally's reply reduced to its outline, for
// telemetry sent before the user has consented to share anything.
func ExplainSafe(c *tally.Client) []string {
	var out []string
	for _, line := range Explain(c) {
		if strings.HasPrefix(line, "Tally replied: ") {
			line = "Tally replied: " + c.LastOutline()
		}
		out = append(out, line)
	}
	return out
}

// LocalTallyHolds reports whether the program on Tally's port is a Tally
// installed on this computer rather than a relay to a cloud Tally.
func LocalTallyHolds(c *tally.Client) bool {
	port := "9000"
	if u, err := url.Parse(c.Address()); err == nil && u.Port() != "" {
		port = u.Port()
	}
	p, _ := strconv.Atoi(port)
	owner := strings.ToLower(support.PortOwner(p))
	return strings.HasPrefix(owner, "tally")
}
