//go:build !windows

package activity

import "time"

func idleFor() (time.Duration, bool) { return 0, false }
