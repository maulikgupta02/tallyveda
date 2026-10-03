package activity

import (
	"syscall"
	"time"
	"unsafe"
)

var (
	user32       = syscall.NewLazyDLL("user32.dll")
	kernel32     = syscall.NewLazyDLL("kernel32.dll")
	getLastInput = user32.NewProc("GetLastInputInfo")
	getTickCount = kernel32.NewProc("GetTickCount")
)

type lastInputInfo struct {
	size uint32
	time uint32
}

func idleFor() (time.Duration, bool) {
	info := lastInputInfo{size: uint32(unsafe.Sizeof(lastInputInfo{}))}
	if r, _, _ := getLastInput.Call(uintptr(unsafe.Pointer(&info))); r == 0 {
		return 0, false
	}
	now, _, _ := getTickCount.Call()
	return time.Duration(uint32(now)-info.time) * time.Millisecond, true
}
