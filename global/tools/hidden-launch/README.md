# Unattended Windows console actions

Build `HiddenLaunch.cs` with the installed .NET Framework C# compiler using
`/target:winexe`. The GUI host itself never allocates a console. Runtime binaries
belong under `.agents/runtime/hidden-launch/<version>`, outside the repository.

`Protect-ConsoleTasks.ps1 -TaskName <names>` reports a plan. Add `-Apply` to replace
only those tasks' actions with the GUI host. It saves original task XML and a
data-only action specification under `.agents/hidden-launch-state`; their ACL
allows the owner, SYSTEM and administrators, without inherited contractor access.
It never starts, stops or deletes the business tasks. Trigger/principal/settings
sections must remain equal to the exported original.

Each original action retains its executable, raw arguments and working directory.
Actions run sequentially with the original user token/environment/session. The
last action's exit code is returned to Task Scheduler. Output goes to a per-run
private log; metadata records only action index, PID, exit code and error type.
No command line is copied to the status log. Task specifications and XML backups
can contain sensitive arguments: do not send them to models or the Knowledge Hub.

The host starts console processes with `CREATE_NO_WINDOW` and, by default, on
`WinSta0\AgentStackBackground`. The latter also keeps a descendant's explicit new
console or GUI window off the input desktop **when default-terminal delegation
uses Windows Console Host**. Windows Terminal delegation was observed creating
its own window on the input desktop even from a noninteractive window station.
`Set-UnattendedConsoleHost.ps1` selects Windows Console Host for the owner account
and retains the original HKCU selection for rollback. Run it in the actual host
context, since an MSIX caller can have a virtualized registry. Windows Terminal
can still be opened explicitly. See [Microsoft's console-host selection](https://support.microsoft.com/en-us/windows/apps/command-prompt-and-windows-powershell).
Never switch the user's desktop to
this desktop. Do not use desktop isolation for a job that controls an existing
interactive Desktop/UI session; `-NoDesktopIsolation` retains the input desktop
and still applies `CREATE_NO_WINDOW` to the original action.

For a new scheduled task, register its definition **disabled**, wrap its actions,
verify the exported sections and then enable it. Do not allow a registration,
logon or due trigger to fire before it has a GUI action. Existing already-GUI
applications and explicitly requested interactive user consoles are distinct.

Rollback does not delete anything: obtain the matching immutable XML backup,
read its original Exec actions and use `Set-ScheduledTask -Action <originals>`.
Recheck triggers/principal/settings, and leave running instances undisturbed.
Changing a Highest task requires an elevated Windows token; do not downgrade it
or bypass UAC. Do not run mail, SMS, backups or other business actions for tests.

Acceptance must cover a synthetic real scheduled task, descendant console/window
creation, input-desktop window/focus observation, error exit, Unicode/space
arguments, working directory and updating an existing running instance.
