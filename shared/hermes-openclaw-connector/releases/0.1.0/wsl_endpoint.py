"""Resolve only the existing Windows WSL virtual interface, never LAN/all NICs."""
import ipaddress
import json
import subprocess

WSL_ALIAS = "vEthernet (WSL (Hyper-V firewall))"
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
QUERY = "@(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -eq 'vEthernet (WSL (Hyper-V firewall))' } | Select-Object IPAddress,PrefixLength,InterfaceAlias) | ConvertTo-Json -Compress"
RFC1918 = tuple(map(ipaddress.ip_network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))


def validate_interface(rows):
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or len(rows) != 1:
        raise ValueError("wsl_interface_ambiguous_or_missing")
    row = rows[0]
    if not isinstance(row, dict) or row.get("InterfaceAlias") != WSL_ALIAS:
        raise ValueError("not_wsl_interface")
    addr = ipaddress.IPv4Address(row.get("IPAddress"))
    prefix = row.get("PrefixLength")
    if type(prefix) is not int or not 16 <= prefix <= 30:
        raise ValueError("wsl_prefix_invalid")
    network = ipaddress.IPv4Network((addr, prefix), strict=False)
    if not any(network.subnet_of(n) for n in RFC1918):
        raise ValueError("wsl_interface_not_private")
    if addr in (network.network_address, network.broadcast_address):
        raise ValueError("wsl_address_invalid")
    return str(addr), str(network)


def resolve_interface():
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", QUERY],
        capture_output=True, text=True, timeout=10, shell=False,
    )
    if result.returncode or len(result.stdout) > 4096:
        raise ValueError("wsl_interface_query_failed")
    return validate_interface(json.loads(result.stdout))
