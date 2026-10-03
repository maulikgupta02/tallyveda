package monitor

import "syscall"

// tryLock opens p with no sharing allowed; Windows closes the handle, and so
// releases the lock, when the process exits.
func tryLock(p string) (func(), bool) {
	name, err := syscall.UTF16PtrFromString(p)
	if err != nil {
		return nil, false
	}
	h, err := syscall.CreateFile(name, syscall.GENERIC_READ|syscall.GENERIC_WRITE, 0, nil,
		syscall.OPEN_ALWAYS, syscall.FILE_ATTRIBUTE_NORMAL, 0)
	if err != nil {
		return nil, false
	}
	return func() { syscall.CloseHandle(h) }, true
}
