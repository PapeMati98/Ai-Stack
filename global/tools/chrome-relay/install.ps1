[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$InstallRoot,
    [Parameter(Mandatory=$true)][string]$PythonExe,
    [string]$SourceRoot = $PSScriptRoot,
    [ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$')][string]$Version = '1.2.0-billingo3',
    [string]$ClientCapabilityPath,
    [string]$NativeSessionId,
    [string]$ProfileId,
    [string]$BrokerBase,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
foreach ($entry in @($InstallRoot,$PythonExe,$SourceRoot)) {
    if (-not [IO.Path]::IsPathRooted($entry)) { throw 'Paths must be absolute.' }
}
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) { throw 'Python executable is absent.' }
$resolvedSource = [IO.Path]::GetFullPath($SourceRoot)
$resolvedRoot = [IO.Path]::GetFullPath($InstallRoot)
$target = [IO.Path]::GetFullPath((Join-Path $resolvedRoot $Version))
if (-not $target.StartsWith($resolvedRoot.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)) {
    throw 'Version destination is outside installation root.'
}
$assignment = @($ClientCapabilityPath,$NativeSessionId,$ProfileId,$BrokerBase)
$assignedCount = @($assignment | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
if ($assignedCount -ne 0 -and $assignedCount -ne 4) { throw 'Assignment requires ClientCapabilityPath, NativeSessionId, ProfileId and BrokerBase together.' }
if ($assignedCount -eq 4) {
    if (-not [IO.Path]::IsPathRooted($ClientCapabilityPath) -or -not (Test-Path -LiteralPath $ClientCapabilityPath -PathType Leaf)) { throw 'ClientCapabilityPath must be an existing absolute private file.' }
    if ($NativeSessionId -notmatch '^[A-Za-z0-9._:-]{1,128}$' -or $ProfileId -notmatch '^[A-Za-z0-9._:-]{1,128}$') { throw 'Invalid explicit assignment.' }
    $uri = [Uri]$BrokerBase
    if ($uri.Scheme -ne 'http' -or $uri.Host -ne '127.0.0.1' -or $uri.IsDefaultPort -or $uri.AbsolutePath -ne '/' -or $uri.Query -or $uri.Fragment -or $uri.UserInfo) { throw 'BrokerBase must be an exact loopback HTTP origin with port.' }
    try {
        if ((Get-Item -LiteralPath $ClientCapabilityPath).Length -gt 65536) { throw 'bound' }
        $client = [IO.File]::ReadAllText($ClientCapabilityPath,[Text.UTF8Encoding]::new($false,$true)) | ConvertFrom-Json
        $keys = @($client.PSObject.Properties.Name | Sort-Object)
        if (($keys -join ',') -ne 'broker_base,profile_id,session_id,token' -or
            $client.session_id -cne $NativeSessionId -or $client.profile_id -cne $ProfileId -or
            $client.broker_base -cne $BrokerBase.TrimEnd('/') -or
            $client.token -isnot [string] -or $client.token -cnotmatch '^[A-Za-z0-9._~+/-]{16,256}={0,2}$') { throw 'binding' }
    } catch { throw 'Invalid per-session capability or explicit assignment mismatch.' }
}
$files = @('broker.py','mcp.py','cli.py','managed.py','registration.py','README.md','install.ps1','uat.py',
    'extension\manifest.json','extension\sw.js','extension\login_gate.js','tests\client.py',
    'tests\test_saved_login.py','tests\test_saved_login.cjs','tests\test_login_gate.cjs','tests\test_required_permission.cjs','tests\test_broker.py','tests\test_mcp.py','tests\test_managed.py','tests\test_registration.py','tests\test_extension.cjs',
    'tests\fixtures\app.html','tests\fixtures\browser.cjs')
$manifest = [ordered]@{}
foreach ($relative in $files) {
    $source = Join-Path $resolvedSource $relative
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Required package file is absent: $relative" }
    $manifest[$relative] = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant()
}
$existing = Test-Path -LiteralPath $target
if ($existing) {
    foreach ($relative in $files) {
        $destination = Join-Path $target $relative
        if (-not (Test-Path -LiteralPath $destination -PathType Leaf) -or
            (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant() -ne $manifest[$relative]) {
            throw 'Installed version has different bytes. Choose a new version; existing files are preserved.'
        }
    }
}
if ($DryRun) {
    [pscustomobject]@{status='DRY_RUN';root=$target;version=$Version;source_sha256=$manifest;
        global_mcp_modified=$false;files_written=0;files_deleted=0} | ConvertTo-Json -Depth 5 -Compress
    return
}
if (-not $existing) {
    [void](New-Item -ItemType Directory -Path $target -Force)
    foreach ($relative in $files) {
        $destination = Join-Path $target $relative
        [void](New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force)
        Copy-Item -LiteralPath (Join-Path $resolvedSource $relative) -Destination $destination
    }
}
$launcher = @'
"""Assigned process only. Reads one private capability, never controller policy."""
import argparse, json, os, subprocess, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from broker import MAX_BODY, exact, ident, parse_json
from mcp import Client
def main():
    p=argparse.ArgumentParser()
    p.add_argument('--client-file',required=True,type=Path)
    p.add_argument('--session-id',required=True)
    p.add_argument('--profile-id',required=True)
    p.add_argument('--broker-base',required=True)
    p.add_argument('--transport',choices=['mcp','cli'],default='mcp')
    a=p.parse_args()
    if not a.client_file.is_absolute(): raise ValueError('absolute_capability_required')
    with a.client_file.open('rb') as stream:
        capability=parse_json(stream.read(MAX_BODY+1))
    exact(capability,('broker_base','session_id','profile_id','token'))
    ident(a.session_id); ident(a.profile_id)
    if capability['profile_id']!=a.profile_id or capability['session_id']!=a.session_id or capability['broker_base']!=a.broker_base.rstrip('/'):
        raise ValueError('explicit_assignment_mismatch')
    Client(capability['broker_base'],capability['session_id'],capability['token'])
    env=dict(os.environ,CHROME_RELAY_BROKER=capability['broker_base'],
             CHROME_RELAY_SESSION_ID=capability['session_id'],CHROME_RELAY_TOKEN=capability['token'],
             PYTHONDONTWRITEBYTECODE='1',PYTHONIOENCODING='utf-8')
    env.pop('CHROME_RELAY_CLIENT_FILE',None)
    command=[sys.executable,'-B',str(ROOT/(a.transport+'.py'))]
    if a.transport=='cli': command.append('call')
    return subprocess.call(command,cwd=ROOT,env=env,
                           stdin=sys.stdin.buffer,stdout=sys.stdout.buffer,stderr=sys.stderr.buffer,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
if __name__=='__main__':
    try: raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'status':'denied','error_type':type(exc).__name__}),file=sys.stderr)
        raise SystemExit(1)
'@
$launcherPath = Join-Path $target 'launch-session.py'
if (Test-Path -LiteralPath $launcherPath) {
    if ([IO.File]::ReadAllText($launcherPath) -ne $launcher) { throw 'Existing launcher differs. Use a new version.' }
} else { [IO.File]::WriteAllText($launcherPath,$launcher,[Text.UTF8Encoding]::new($false)) }
$manifest['launch-session.py'] = (Get-FileHash -LiteralPath $launcherPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($assignedCount -eq 4) {
    $binding = [ordered]@{command=$PythonExe;args=@('-B',$launcherPath,'--client-file',[IO.Path]::GetFullPath($ClientCapabilityPath),
        '--session-id',$NativeSessionId,'--profile-id',$ProfileId,'--broker-base',$BrokerBase,'--transport','mcp');
        cli_transport='Replace final transport value with cli; provide the same semantic request over stdin.';
        launch_requires='Native host must set CREATE_NO_WINDOW and preserve stdio pipes.';
        profile_id=$ProfileId;native_session_id=$NativeSessionId;active=$false}
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $assignmentId = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($NativeSessionId)))).Replace('-','').ToLowerInvariant().Substring(0,16) }
    finally { $sha.Dispose() }
    $bindingPath = Join-Path $target ('assigned-process-'+$assignmentId+'.json')
    $bindingText = $binding | ConvertTo-Json -Depth 5
    if ((Test-Path -LiteralPath $bindingPath) -and [IO.File]::ReadAllText($bindingPath) -ne $bindingText) { throw 'Existing process assignment differs; it is preserved.' }
    if (-not (Test-Path -LiteralPath $bindingPath)) { [IO.File]::WriteAllText($bindingPath,$bindingText,[Text.UTF8Encoding]::new($false)) }
}
$previous = @(Get-ChildItem -LiteralPath $resolvedRoot -Directory | Where-Object { $_.FullName -ne $target } | ForEach-Object { $_.FullName })
$receipt = [ordered]@{status='STAGED_INERT';version=$Version;root=$target;source_sha256=$manifest;
    python_exe=$PythonExe;previous_versions=$previous;rollback='Select the retained previous runtime path in the assigned process configuration.';
    broker_started=$false;extension_activated=$false;global_mcp_modified=$false;autostart_modified=$false;
    policy_modified=$false;files_deleted=0;staged_at=[DateTime]::UtcNow.ToString('o')}
$receiptPath = Join-Path $target 'install-receipt.json'
if (-not (Test-Path -LiteralPath $receiptPath)) { [IO.File]::WriteAllText($receiptPath,($receipt|ConvertTo-Json -Depth 5),[Text.UTF8Encoding]::new($false)) }
[pscustomobject]@{status='STAGED_INERT';root=$target;version=$Version;global_mcp_modified=$false;files_deleted=0} | ConvertTo-Json -Compress
