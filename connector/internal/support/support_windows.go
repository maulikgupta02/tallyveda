package support

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

func powershell(script string, limit time.Duration) string {
	ctx, cancel := context.WithTimeout(context.Background(), limit)
	defer cancel()
	cmd := exec.CommandContext(ctx, "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script)
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000} // CREATE_NO_WINDOW
	out, err := cmd.CombinedOutput()
	if err != nil && len(out) == 0 {
		return "powershell failed: " + err.Error() + "\n"
	}
	return string(out)
}

const reportScript = `
$ErrorActionPreference = 'SilentlyContinue'
$os = Get-CimInstance Win32_OperatingSystem
"Windows: $($os.Caption) $($os.Version) $($os.OSArchitecture)"
"Memory: {0:N1} GB total, {1:N1} GB free" -f ($os.TotalVisibleMemorySize/1MB), ($os.FreePhysicalMemory/1MB)
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
"CPU: $($cpu.Name), $($cpu.NumberOfLogicalProcessors) threads"
$found = $false
Get-Process | Where-Object { $_.ProcessName -match '^tally' } | ForEach-Object {
  $found = $true
  $v = $_.MainModule.FileVersionInfo
  "Tally process: $($_.ProcessName) (pid $($_.Id)) at $($_.Path)"
  "  product: $($v.ProductName) $($v.ProductVersion), file version $($v.FileVersion)"
  "  memory {0:N0} MB, CPU time {1:N0} s, responding to Windows: {2}, window title: {3}" -f ($_.WorkingSet64/1MB), $_.CPU, $_.Responding, $_.MainWindowTitle
  $dir = Split-Path $_.Path
  foreach ($name in 'tally.ini', 'TallyPrime.ini') {
    $ini = Join-Path $dir $name
    if (Test-Path $ini) {
      "  ${name}:"
      Get-Content $ini | Where-Object { $_ -match '^\s*\[|^\s*(TDL|User TDL|Default TDL|Load|Data|Port|Client Server|ODBC|Language|Default Companies)' } | ForEach-Object { "    $_" }
    }
  }
}
if (-not $found) { "Tally process: not found" }
$l = Get-NetTCPConnection -LocalPort 9000 -State Listen
if ($l) { foreach ($c in $l) { $p = Get-Process -Id $c.OwningProcess; "Port 9000 is served by $($p.ProcessName) (pid $($c.OwningProcess))" } } else { "Port 9000: nothing listening" }
`

func gather() string { return powershell(reportScript, 30*time.Second) }

// sample returns Tally's total CPU seconds and a one-line status.
func sample() (float64, string) {
	out := strings.TrimSpace(powershell(`$ErrorActionPreference='SilentlyContinue'
$p = Get-Process | Where-Object { $_.ProcessName -match '^tally' } | Select-Object -First 1
if ($p) { "{0}|Tally memory {1:N0} MB, CPU time {0:N1} s, responding to Windows: {2}" -f $p.CPU, ($p.WorkingSet64/1MB), $p.Responding } else { "0|Tally process not found" }`, 15*time.Second))
	cpu, line, ok := strings.Cut(out, "|")
	if !ok {
		return 0, out
	}
	v, _ := strconv.ParseFloat(strings.ReplaceAll(cpu, ",", ""), 64)
	return v, line
}

// screenshot captures only the Tally window, as a JPEG.
func screenshot() []byte {
	out := filepath.Join(os.TempDir(), "tallyconnector-window.jpg")
	os.Remove(out)
	powershell(`$ErrorActionPreference='SilentlyContinue'
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System; using System.Runtime.InteropServices;
public class TCWin {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
}
"@
$p = Get-Process | Where-Object { $_.ProcessName -match '^tally' -and $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if ($p -and -not [TCWin]::IsIconic($p.MainWindowHandle)) {
  $r = New-Object TCWin+RECT
  [void][TCWin]::GetWindowRect($p.MainWindowHandle, [ref]$r)
  $w = $r.R - $r.L; $h = $r.B - $r.T
  if ($w -gt 0 -and $h -gt 0) {
    $b = New-Object System.Drawing.Bitmap $w, $h
    $g = [System.Drawing.Graphics]::FromImage($b)
    $g.CopyFromScreen($r.L, $r.T, 0, 0, $b.Size)
    $enc = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.MimeType -eq 'image/jpeg' }
    $prm = New-Object System.Drawing.Imaging.EncoderParameters 1
    $prm.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter ([System.Drawing.Imaging.Encoder]::Quality), 55L
    $b.Save('`+out+`', $enc, $prm)
  }
}`, 30*time.Second)
	img, _ := os.ReadFile(out)
	os.Remove(out)
	return img
}
