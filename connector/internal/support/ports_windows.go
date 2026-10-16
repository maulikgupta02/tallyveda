package support

import (
	"fmt"
	"path/filepath"
	"syscall"
	"unsafe"
)

var getExtendedTCPTable = syscall.NewLazyDLL("iphlpapi.dll").NewProc("GetExtendedTcpTable")

const (
	afInet               = 2
	afInet6              = 23
	tcpTableOwnerPIDList = 3 // TCP_TABLE_OWNER_PID_LISTENER
)

// PortOwner names the program listening on a local TCP port, e.g.
// `tally.exe (C:\Program Files\TallyPrime\tally.exe, pid 4120)`, or "" when
// nothing is listening or Windows won't say.
func PortOwner(port int) (owner string) {
	defer func() {
		if recover() != nil {
			owner = ""
		}
	}()
	for _, af := range []uintptr{afInet, afInet6} {
		if pid := listenerPID(af, port); pid != 0 {
			path := processPath(pid)
			name := filepath.Base(path)
			if path == "" {
				name, path = "unknown program", "path hidden"
			}
			return fmt.Sprintf("%s (%s, pid %d)", name, path, pid)
		}
	}
	return ""
}

func listenerPID(af uintptr, port int) uint32 {
	var size uint32
	getExtendedTCPTable.Call(0, uintptr(unsafe.Pointer(&size)), 0, af, tcpTableOwnerPIDList, 0)
	if size == 0 {
		return 0
	}
	buf := make([]byte, size+1024)
	size = uint32(len(buf))
	if r, _, _ := getExtendedTCPTable.Call(uintptr(unsafe.Pointer(&buf[0])), uintptr(unsafe.Pointer(&size)), 0, af, tcpTableOwnerPIDList, 0); r != 0 {
		return 0
	}
	n := *(*uint32)(unsafe.Pointer(&buf[0]))
	// MIB_TCPROW_OWNER_PID is 6 DWORDs; MIB_TCP6ROW_OWNER_PID is 56 bytes.
	// Rows start after dwNumEntries; the port is network order in the low word.
	rowSize, portOff, pidOff := 24, 8, 20
	if af == afInet6 {
		rowSize, portOff, pidOff = 56, 20, 52
	}
	for i := 0; i < int(n); i++ {
		row := 4 + i*rowSize
		if row+rowSize > len(buf) {
			break
		}
		raw := *(*uint32)(unsafe.Pointer(&buf[row+portOff]))
		if int(uint16(raw>>8)&0xff|uint16(raw&0xff)<<8) == port {
			return *(*uint32)(unsafe.Pointer(&buf[row+pidOff]))
		}
	}
	return 0
}

// LocalTally lists Tally programs running on this computer itself.
func LocalTally() []string {
	var out []string
	for _, p := range tallyProcesses() {
		out = append(out, fmt.Sprintf("%s (%s, pid %d)", p.name, p.path, p.pid))
	}
	return out
}
