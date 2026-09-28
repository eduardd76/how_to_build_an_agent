# Network configuration standard (sample)

Synthetic sample standard for the Config review template. Replace it with your organisation's standard.
Each rule id matches a finding from the `config_lint` tool.

## CFG-MGMT-01 — SSH only on management lines
Telnet sends credentials in clear text. Fix: `transport input ssh` on every VTY line. Severity: high.

## CFG-MGMT-02 — No plain HTTP management
Fix: `no ip http server`; use `ip http secure-server` only if the web interface is needed. Severity: medium.

## CFG-SNMP-01 — No default SNMP communities
`public` and `private` are the first strings an attacker tries. Fix: move to SNMPv3 with authentication and privacy. Severity: high.

## CFG-SNMP-02 — No read-write SNMP
A read-write community lets anyone who knows it change the device. Fix: remove RW communities; make changes through the change process. Severity: high.

## CFG-AUTH-01 — Password encryption service
Fix: `service password-encryption`. This only obscures type 7 passwords; prefer secrets (CFG-AUTH-02, CFG-AUTH-03). Severity: medium.

## CFG-AUTH-02 — Enable secret, never enable password
Fix: replace `enable password` with `enable secret` using type 9 (scrypt). Severity: high.

## CFG-AUTH-03 — Local users with secrets
Fix: `username NAME secret 9 ...` instead of `password`. Severity: medium.

## CFG-ACL-01 — No permit any any
An ACL that ends in `permit ip any any` filters nothing after its explicit entries. Fix: permit only required traffic and end with an explicit deny that logs. Severity: high.

## CFG-LOG-01 — Remote syslog
Fix: `logging host <collector>` so events survive a device reload. Severity: low.

## CFG-NTP-01 — Time synchronisation
Fix: `ntp server <server>`; logs without correct time cannot be correlated. Severity: low.

Changes are made through the normal change process after review. This standard never authorises an agent to change a device.
