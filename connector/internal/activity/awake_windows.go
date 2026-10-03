package activity

import (
	"runtime"
	"syscall"
)

var setExecState = syscall.NewLazyDLL("kernel32.dll").NewProc("SetThreadExecutionState")

const (
	esContinuous     = 0x80000000
	esSystemRequired = 0x00000001
)

// KeepAwake stops Windows from sleeping on idle until the returned func is
// called. The request belongs to one OS thread, so a locked goroutine holds it.
func KeepAwake() func() {
	release := make(chan struct{})
	done := make(chan struct{})
	go func() {
		runtime.LockOSThread()
		defer runtime.UnlockOSThread()
		setExecState.Call(uintptr(esContinuous | esSystemRequired))
		<-release
		setExecState.Call(uintptr(esContinuous))
		close(done)
	}()
	return func() { close(release); <-done }
}
