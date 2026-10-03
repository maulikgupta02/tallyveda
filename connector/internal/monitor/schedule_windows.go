package monitor

import (
	"fmt"
	"html"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
	"unicode/utf16"
)

const taskName = "TallyConnector Daily Update"

// Task definition as XML: schtasks' command-line flags can't set
// StartWhenAvailable (run after a missed start) or the repetition window.
// It runs every 2 hours from 09:00 to 21:00 while the user is logged on; a
// run that isn't due exits after one small HTTPS call.
const taskXML = `<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>%s</Description></RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>%s</StartBoundary>
      <Repetition><Interval>PT2H</Interval><Duration>PT12H</Duration><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT6H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>%s</Command><Arguments>-monitor-run</Arguments></Exec></Actions>
</Task>`

func schedule(exe string) error {
	start := time.Now().AddDate(0, 0, 1).Format("2006-01-02") + "T09:00:00"
	desc := fmt.Sprintf("Sends daily updates of your Tally accounts to the banks you chose. To stop, run "+
		"TallyConnector and click 'Stop daily updates', or run: \"%s\" -monitor-stop", exe)
	def := fmt.Sprintf(taskXML, html.EscapeString(desc), start, html.EscapeString(exe))

	d, err := Dir()
	if err != nil {
		return err
	}
	xmlPath := filepath.Join(d, "task.xml")
	if err := os.WriteFile(xmlPath, utf16le(def), 0o600); err != nil {
		return err
	}
	defer os.Remove(xmlPath)
	if out, err := schtasks("/Create", "/F", "/TN", taskName, "/XML", xmlPath); err != nil {
		return fmt.Errorf("could not create the scheduled task: %v: %s", err, strings.TrimSpace(out))
	}
	return nil
}

func unschedule() error {
	out, err := schtasks("/Delete", "/F", "/TN", taskName)
	if err != nil && !strings.Contains(strings.ToLower(out), "cannot find") {
		return fmt.Errorf("could not remove the scheduled task: %v: %s", err, strings.TrimSpace(out))
	}
	return nil
}

func schtasks(args ...string) (string, error) {
	cmd := exec.Command("schtasks.exe", args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000} // CREATE_NO_WINDOW
	out, err := cmd.CombinedOutput()
	return string(out), err
}

func utf16le(s string) []byte {
	u := utf16.Encode([]rune(s))
	b := []byte{0xFF, 0xFE}
	for _, r := range u {
		b = append(b, byte(r), byte(r>>8))
	}
	return b
}

// Scheduled reports whether the task exists.
func Scheduled() bool {
	_, err := schtasks("/Query", "/TN", taskName)
	return err == nil
}
