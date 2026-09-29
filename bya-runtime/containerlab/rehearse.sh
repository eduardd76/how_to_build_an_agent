#!/usr/bin/env bash
# Break the BYA lab on purpose, so an agent can investigate a realistic incident.
# The agent never runs this: you do. It only touches the lab's own containers (clab-bya-*).
#
#   ./rehearse.sh link-down     shut srl1 ethernet-1/1 (srl1 ↔ srl2); both BGP sessions on that link drop
#   ./rehearse.sh loss          20 % packet loss on srl1 e1-2 (srl1 ↔ frr1)
#   ./rehearse.sh bgp-down      frr1 shuts its BGP session to srl1
#   ./rehearse.sh restore       undo all of the above
#   ./rehearse.sh status        BGP summary on every router
set -euo pipefail
LAB=clab-bya
srl() { docker exec "$LAB-$1" sr_cli "$2"; }
srl_set() { printf 'enter candidate private\n%s\ncommit now\n' "$2" | docker exec -i "$LAB-$1" sr_cli; }
frr() { docker exec "$LAB-frr1" vtysh "$@"; }

case "${1:-}" in
  link-down)
    srl_set srl1 "set / interface ethernet-1/1 admin-state disable" ;;
  loss)
    containerlab tools netem set -n "$LAB-srl1" -i e1-2 --loss 20 ;;
  bgp-down)
    frr -c "configure terminal" -c "router bgp 65003" -c "neighbor 10.0.13.0 shutdown" ;;
  restore)
    srl_set srl1 "set / interface ethernet-1/1 admin-state enable"
    containerlab tools netem set -n "$LAB-srl1" -i e1-2 --loss 0
    frr -c "configure terminal" -c "router bgp 65003" -c "no neighbor 10.0.13.0 shutdown" ;;
  status)
    for n in srl1 srl2; do echo "== $n"; srl "$n" "show network-instance default protocols bgp neighbor"; done
    echo "== frr1"; frr -c "show bgp summary" ;;
  *)
    sed -n '2,10p' "$0"; exit 1 ;;
esac
