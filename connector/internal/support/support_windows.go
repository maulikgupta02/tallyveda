package support

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"unsafe"
)

// Everything here uses plain Win32 calls. Running PowerShell or capturing
// the screen looks like malware to antivirus software, and got the
// connector quarantined on a real install (2026-10-04).

var (
	kernel32                  = syscall.NewLazyDLL("kernel32.dll")
	user32                    = syscall.NewLazyDLL("user32.dll")
	ntdll                     = syscall.NewLazyDLL("ntdll.dll")
	version                   = syscall.NewLazyDLL("version.dll")
	globalMemoryStatusEx      = kernel32.NewProc("GlobalMemoryStatusEx")
	queryFullProcessImageName = kernel32.NewProc("QueryFullProcessImageNameW")
	getProcessMemoryInfo      = kernel32.NewProc("K32GetProcessMemoryInfo")
	rtlGetVersion             = ntdll.NewProc("RtlGetVersion")
	enumWindows               = user32.NewProc("EnumWindows")
	getWindowThreadProcessID  = user32.NewProc("GetWindowThreadProcessId")
	isWindowVisible           = user32.NewProc("IsWindowVisible")
	isHungAppWindow           = user32.NewProc("IsHungAppWindow")
	getFileVersionInfoSize    = version.NewProc("GetFileVersionInfoSizeW")
	getFileVersionInfo        = version.NewProc("GetFileVersionInfoW")
	verQueryValue             = version.NewProc("VerQueryValueW")
)

const processQueryLimitedInformation = 0x1000

type memoryStatusEx struct {
	length, memoryLoad                          uint32
	totalPhys, availPhys, totalPage, availPage  uint64
	totalVirtual, availVirtual, availExtVirtual uint64
}

type osVersionInfo struct {
	size, major, minor, build, platform uint32
	csd                                 [128]uint16
}

type processMemoryCounters struct {
	cb, pageFaultCount               uint32
	peakWorkingSet, workingSet       uintptr
	quotaPeakPaged, quotaPaged       uintptr
	quotaPeakNonPaged, quotaNonPaged uintptr
	pagefileUsage, peakPagefileUsage uintptr
}

type tallyProc struct {
	pid  uint32
	name string
	path string
}

func tallyProcesses() []tallyProc {
	snap, err := syscall.CreateToolhelp32Snapshot(syscall.TH32CS_SNAPPROCESS, 0)
	if err != nil {
		return nil
	}
	defer syscall.CloseHandle(snap)
	var e syscall.ProcessEntry32
	e.Size = uint32(unsafe.Sizeof(e))
	var out []tallyProc
	for err = syscall.Process32First(snap, &e); err == nil; err = syscall.Process32Next(snap, &e) {
		name := syscall.UTF16ToString(e.ExeFile[:])
		lower := strings.ToLower(name)
		if strings.HasPrefix(lower, "tally") && !strings.HasPrefix(lower, "tallyconnector") {
			out = append(out, tallyProc{pid: e.ProcessID, name: name, path: processPath(e.ProcessID)})
		}
	}
	return out
}

func processPath(pid uint32) string {
	h, err := syscall.OpenProcess(processQueryLimitedInformation, false, pid)
	if err != nil {
		return ""
	}
	defer syscall.CloseHandle(h)
	buf := make([]uint16, 1024)
	n := uint32(len(buf))
	if r, _, _ := queryFullProcessImageName.Call(uintptr(h), 0, uintptr(unsafe.Pointer(&buf[0])), uintptr(unsafe.Pointer(&n))); r == 0 {
		return ""
	}
	return syscall.UTF16ToString(buf[:n])
}

// processStats returns CPU seconds and working set in MB.
func processStats(pid uint32) (float64, float64, bool) {
	h, err := syscall.OpenProcess(processQueryLimitedInformation, false, pid)
	if err != nil {
		return 0, 0, false
	}
	defer syscall.CloseHandle(h)
	var created, exited, kernel, user syscall.Filetime
	if syscall.GetProcessTimes(h, &created, &exited, &kernel, &user) != nil {
		return 0, 0, false
	}
	ticks := (uint64(kernel.HighDateTime)<<32 | uint64(kernel.LowDateTime)) +
		(uint64(user.HighDateTime)<<32 | uint64(user.LowDateTime))
	var mem processMemoryCounters
	mem.cb = uint32(unsafe.Sizeof(mem))
	getProcessMemoryInfo.Call(uintptr(h), uintptr(unsafe.Pointer(&mem)), uintptr(mem.cb))
	return float64(ticks) / 1e7, float64(mem.workingSet) / (1 << 20), true
}

// hung reports whether Windows considers a visible window of pid "Not
// responding", and whether it has a visible window at all.
func hung(pid uint32) (isHung, found bool) {
	cb := syscall.NewCallback(func(hwnd, _ uintptr) uintptr {
		var owner uint32
		getWindowThreadProcessID.Call(hwnd, uintptr(unsafe.Pointer(&owner)))
		if owner != pid {
			return 1
		}
		if v, _, _ := isWindowVisible.Call(hwnd); v == 0 {
			return 1
		}
		found = true
		if h, _, _ := isHungAppWindow.Call(hwnd); h != 0 {
			isHung = true
		}
		return 1
	})
	enumWindows.Call(cb, 0)
	return isHung, found
}

func fileVersion(path string) string {
	p, err := syscall.UTF16PtrFromString(path)
	if err != nil {
		return ""
	}
	size, _, _ := getFileVersionInfoSize.Call(uintptr(unsafe.Pointer(p)), 0)
	if size == 0 {
		return ""
	}
	buf := make([]byte, size)
	if r, _, _ := getFileVersionInfo.Call(uintptr(unsafe.Pointer(p)), 0, size, uintptr(unsafe.Pointer(&buf[0]))); r == 0 {
		return ""
	}
	root, _ := syscall.UTF16PtrFromString(`\`)
	var info *[13]uint32 // VS_FIXEDFILEINFO
	var n uint32
	if r, _, _ := verQueryValue.Call(uintptr(unsafe.Pointer(&buf[0])), uintptr(unsafe.Pointer(root)),
		uintptr(unsafe.Pointer(&info)), uintptr(unsafe.Pointer(&n))); r == 0 || info == nil {
		return ""
	}
	ms, ls := info[2], info[3] // dwFileVersionMS, dwFileVersionLS
	return fmt.Sprintf("%d.%d.%d.%d", ms>>16, ms&0xffff, ls>>16, ls&0xffff)
}

func gather() string {
	var b strings.Builder
	var v osVersionInfo
	v.size = uint32(unsafe.Sizeof(v))
	rtlGetVersion.Call(uintptr(unsafe.Pointer(&v)))
	fmt.Fprintf(&b, "Windows: %d.%d build %d, connector %s\n", v.major, v.minor, v.build, runtime.GOARCH)
	var m memoryStatusEx
	m.length = uint32(unsafe.Sizeof(m))
	if r, _, _ := globalMemoryStatusEx.Call(uintptr(unsafe.Pointer(&m))); r != 0 {
		fmt.Fprintf(&b, "Memory: %.1f GB total, %.1f GB free (%d%% in use)\n",
			float64(m.totalPhys)/(1<<30), float64(m.availPhys)/(1<<30), m.memoryLoad)
	}
	fmt.Fprintf(&b, "CPU threads: %d\n", runtime.NumCPU())
	procs := tallyProcesses()
	if len(procs) == 0 {
		b.WriteString("Tally process: not found\n")
	}
	for _, p := range procs {
		fmt.Fprintf(&b, "Tally process: %s (pid %d) at %s\n", p.name, p.pid, p.path)
		if p.path == "" {
			continue
		}
		if ver := fileVersion(p.path); ver != "" {
			fmt.Fprintf(&b, "  file version: %s\n", ver)
		}
		if st, err := os.Stat(p.path); err == nil {
			fmt.Fprintf(&b, "  exe modified %s, %d MB\n", st.ModTime().Format("2006-01-02"), st.Size()>>20)
		}
		if cpu, mem, ok := processStats(p.pid); ok {
			isHung, found := hung(p.pid)
			fmt.Fprintf(&b, "  memory %.0f MB, CPU time %.1f s, has window %v, not responding %v\n", mem, cpu, found, isHung)
		}
		for _, name := range []string{"tally.ini", "TallyPrime.ini"} {
			raw, err := os.ReadFile(filepath.Join(filepath.Dir(p.path), name))
			if err != nil {
				continue
			}
			fmt.Fprintf(&b, "  %s:\n", name)
			for _, line := range strings.Split(string(raw), "\n") {
				t := strings.TrimSpace(line)
				l := strings.ToLower(t)
				for _, k := range []string{"[", "tdl", "user tdl", "default tdl", "load", "data", "port", "client server", "odbc", "default companies"} {
					if strings.HasPrefix(l, k) {
						fmt.Fprintf(&b, "    %s\n", t)
						break
					}
				}
			}
		}
	}
	return b.String()
}

// sample returns the main Tally process's CPU seconds and a one-line status.
func sample() (float64, string) {
	for _, p := range tallyProcesses() {
		if !strings.EqualFold(strings.TrimSuffix(p.name, ".exe"), "tally") {
			continue
		}
		cpu, mem, ok := processStats(p.pid)
		if !ok {
			return 0, ""
		}
		isHung, _ := hung(p.pid)
		return cpu, fmt.Sprintf("Tally memory %.0f MB, CPU time %.1f s, not responding %v", mem, cpu, isHung)
	}
	return 0, "Tally process not found"
}
