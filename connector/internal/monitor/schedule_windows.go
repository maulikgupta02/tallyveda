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
// It runs when the user signs in to Windows and every 15 minutes all day, so a
// sync starts soon after Tally is opened. A run with nothing to do exits after
// checking that Tally answers (and, when it does, one small HTTPS call).
const taskXML = `<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>%s</Description></RegistrationInfo>
  <Triggers>%s
    <CalendarTrigger>
      <StartBoundary>%s</StartBoundary>
      <Repetition><Interval>PT15M</Interval><Duration>P1D</Duration><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
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

const logonTrigger = `
    <LogonTrigger><Enabled>true</Enabled><UserId>%s</UserId><Delay>PT2M</Delay></LogonTrigger>`

func schedule(exe string) error {
	// Midnight today: a start in the past just means the next 15-minute slot.
	start := time.Now().Format("2006-01-02") + "T00:00:00"
	desc := fmt.Sprintf("Sends daily updates of your Tally accounts to the organisation you chose. To stop, open "+
		"TallyVeda (TallyConnector.exe) and click 'Stop daily updates', or run: \"%s\" -monitor-stop", exe)
	user := os.Getenv("USERNAME")
	if dom := os.Getenv("USERDOMAIN"); dom != "" && user != "" {
		user = dom + `\` + user
	}
	triggers := []string{""}
	if user != "" {
		triggers = []string{fmt.Sprintf(logonTrigger, html.EscapeString(user)), ""}
	}
	d, err := Dir()
	if err != nil {
		return err
	}
	xmlPath := filepath.Join(d, "task.xml")
	defer os.Remove(xmlPath)
	var lastErr error
	// Some locked-down PCs refuse a sign-in trigger without admin rights; the
	// 15-minute schedule alone still works there.
	for _, trig := range triggers {
		def := fmt.Sprintf(taskXML, html.EscapeString(desc), trig, start, html.EscapeString(exe))
		if err := os.WriteFile(xmlPath, utf16le(def), 0o600); err != nil {
			return err
		}
		out, err := schtasks("/Create", "/F", "/TN", taskName, "/XML", xmlPath)
		if err == nil {
			return nil
		}
		lastErr = fmt.Errorf("could not create the scheduled task: %v: %s", err, strings.TrimSpace(out))
	}
	return lastErr
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
