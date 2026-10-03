//go:build !windows

package activity

// KeepAwake is a no-op off Windows.
func KeepAwake() func() { return func() {} }
