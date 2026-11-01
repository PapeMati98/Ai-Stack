# Guest runtime reference

This reference describes the DOTS Windows connector. Installation and native
acceptance belong to the runtime maintainer; ordinary browser tasks reuse the
registered tools and assigned profile without running the installer again.

The names below are retained technical compatibility IDs, not configured
accounts or authorization to access a company. A new deployment must provision
its own profiles and policy; this source alone does not activate DOTS.

## Stable assignment

Hermes loads one MCP stdio process per DOT. The MCP configuration fixes the
profile ID in its environment; model tool arguments cannot select a profile
or tab. DOT profile IDs are stable (`umbrella-observer-chrome-v1` and
`finance-collector-chrome-v1`) across cron session changes. The broker and
extension store their attachment independently of a Hermes session ID.

The broker runs as the current limited user through the reviewed GUI-subsystem
`HiddenLaunch-SystemStation.exe`. The supported Scheduled Task configuration is
`InteractiveToken` and `Limited` under `dots-desktop`, with `IgnoreNew` and
the existing recovery triggers. Browser tools require that user's logged-in
desktop session; they are unavailable after logoff until the session returns.
The API/mail routines use their separate batch controller and continue
independently. No extra batch-logon privilege is granted to the desktop user.
The broker binds only to `127.0.0.1`; it exposes no remote debugging port.

The names below are retained technical compatibility IDs, not configured
accounts or authorization to access a company. A new deployment must provision
its own profiles and policy; this source alone does not activate DOTS.

## Public package and guest parameters

The runtime's `guest_install.ps1` requires absolute paths for
`-SourceRoot`, `-PythonExe` (Python 3.14), `-ChromiumExe` (Chrome-for-Testing
from the pinned runtime manifest), and `-HiddenLauncherExe` (the exact
verified SystemStation binary), plus `-DotId` from the fixed pair
`umbrella-observer` / `finance-collector`.

Inert staging omits all scope fields. Owner-authorized provisioning supplies
the exact `-ExtensionId`, one or more bare HTTPS `-AllowedOrigin` values, and
one or more `-AllowedAction` values. The command never reads the normal Chrome
profile and never infers production origins. Pairing is generated in the guest
and passed to its registration helper over stdin, never on a process command
line or in terminal output.

The names below are retained technical compatibility IDs, not configured
accounts or authorization to access a company. A new deployment must provision
its own profiles and policy; this source alone does not activate DOTS.

## Synthetic UAT

The runtime's `guest_uat.ps1` accepts explicit `-PythonExe`,
`-ChromiumExe`, `-ConnectorRoot`, `-ExtensionSource`, `-FixturePath`, and
`-UatScript`. It creates fresh disposable profiles and a localhost HTTPS
fixture, loads the real MV3 extension, and exercises the actual MCP bridge
through a synthetic CDP controller. It does not select any owner profile or
account, and it needs no paid model or external network. The first toolbar
attach in a live profile remains a human action; synthetic UAT attaches only
the synthetic fixture through its fixed test hook.
