//go:build !windows

package monitor

import "errors"

// Tally only runs on Windows; on other systems (development) the settings
// are saved but nothing is scheduled. Run `tallyconnector -monitor-run` from
// cron to exercise the refresh.
var errNoScheduler = errors.New("automatic scheduling is only available on Windows; run -monitor-run periodically instead")

func schedule(exe, bank string) error { return errNoScheduler }
func unschedule() error               { return nil }
func Scheduled() bool                 { return false }
