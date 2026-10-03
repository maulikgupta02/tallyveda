// Package activity tells the connector whether someone is using the computer,
// so it can go easy on Tally while they work and speed up when they don't.
package activity

import "time"

// Busy reports whether the keyboard or mouse was used within window. Where
// that can't be told (non-Windows, or the call fails) it assumes yes.
func Busy(window time.Duration) bool {
	idle, ok := idleFor()
	return !ok || idle < window
}
