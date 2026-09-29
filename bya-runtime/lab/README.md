# Recorded device outputs

Sample mode never contacts a device. The **Device commands** block replays the outputs in this folder:
`lab/<device>/<command>.txt`, where the file name is the command in lower case with every run of other
characters replaced by `_` (for example `show ip interface brief` → `show_ip_interface_brief.txt`).
`commands.json` lists each device's recorded commands; the agent is shown that list when it asks for one that isn't recorded.

The outputs here are synthetic. Add recordings from your own devices (with secrets removed) to build eval
cases that replay real incidents.
