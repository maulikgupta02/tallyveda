//go:build !windows

package support

import "runtime"

func gather() string {
	return "OS: " + runtime.GOOS + " (system details are collected on Windows only)\n"
}
func sample() (float64, string) { return 0, "" }
func screenshot() []byte        { return nil }
