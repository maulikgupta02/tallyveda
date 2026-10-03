//go:build !windows

package monitor

import "errors"

// There is no non-Windows scheduled-task API, so on Linux/macOS the settings
// are saved but nothing is scheduled automatically. For a Linux cloud install
// (e.g. an AWS instance running the connector against a Tally reachable over
// the network), run `TallyConnector -monitor-run` from cron or a systemd timer
// instead — see "Daily monitoring" in README.md for a ready-to-use crontab
// line and systemd unit.
var errNoScheduler = errors.New("automatic scheduling is only available on Windows; run -monitor-run periodically from cron or a systemd timer instead (see README.md)")

func schedule(exe string) error { return errNoScheduler }
func unschedule() error         { return nil }
func Scheduled() bool           { return false }
