# BYA lab on containerlab

A three-router lab (two Nokia SR Linux, one FRR, eBGP in a triangle) that BYA agents can read for real, and that
you can break on purpose to rehearse incidents. Everything the agent does is read-only; only `rehearse.sh`, run by
you, changes the lab.

```
srl1 (AS 65001) ──e1-1──── e1-1── srl2 (AS 65002)
   e1-2                           e1-2
      └── eth1 ── frr1 (AS 65003) ── eth2 ┘
```

## 1. Deploy (macOS with Docker Desktop, or Linux)

containerlab is a Linux program. On macOS, run it from its own container against Docker Desktop's engine.
Run these from `bya-runtime/containerlab/`:

```sh
docker pull ghcr.io/nokia/srlinux:latest
docker pull quay.io/frrouting/frr:10.1.0

alias clab='docker run --rm -it --privileged --network host --pid host \
  -v /var/run/docker.sock:/var/run/docker.sock -v /var/run/netns:/var/run/netns \
  -v /var/lib/docker/containers:/var/lib/docker/containers \
  -v "$(pwd)":"$(pwd)" -w "$(pwd)" ghcr.io/srl-labs/clab containerlab'

clab deploy -t bya.clab.yml
```

On Linux with containerlab installed, use `sudo containerlab deploy -t bya.clab.yml` instead.

The lab uses its own management network, `bya-mgmt` (172.29.29.0/24). If deploy reports that the subnet overlaps an
existing Docker network, change `ipv4-subnet` in `bya.clab.yml` to a free /24.

Deploying writes `clab-bya/topology-data.json` next to the topology. BYA reads the node names and kinds from it.
SR Linux takes a minute or two to boot. Check BGP with `./rehearse.sh status` (all sessions `established`).

## 2. Record real output for sample mode

From `bya-runtime/`:

```sh
python3 -m bya.graph record containerlab/clab-bya/topology-data.json
```

Runs the commands in `containerlab/record-commands.json` on every node (through the read-only filter, with
`docker exec` and the node's own CLI) and saves them to `lab/<node>/`, secrets masked. Sample mode and evals then
replay real SR Linux and FRR output. Run it again after `rehearse.sh` to record a broken state as well.

## 3. Build an agent that reads the lab

In the studio: **New agent** → any kind of job → **Devices from: My containerlab lab** → tick **BGP neighbours** and
**Interface status and counters**. **Reach** lists `srl1, srl2, frr1`.

In live mode BYA runs each allowed command as `docker exec clab-bya-<node> sr_cli "<command>"` (SR Linux),
`vtysh -c "<command>"` (FRR) or `Cli -c "<command>"` (cEOS). No SSH keys or management-IP routing are needed; BYA
must run where the `docker` command reaches the lab's engine (on macOS, your Mac).

## 4. Rehearse an incident

```sh
./rehearse.sh bgp-down     # frr1 shuts its session to srl1
```

Run the agent (live mode, or record first and use sample mode), check its brief names the right peer, then
`./rehearse.sh restore`. Faults: `link-down`, `loss` (20 % on srl1 ↔ frr1), `bgp-down`.

## Notes

- The startup configs use SR Linux CLI `set` syntax. If your SR Linux release rejects a line, check the release's
  CLI reference; the lab was written for recent releases and has not been booted in BYA's CI.
- On Apple Silicon, check that the SR Linux release you pull publishes an arm64 image. FRR is multi-arch.
- `docker exec` access is root-equivalent on that Docker engine. Point BYA at lab engines only.
- Destroy with `clab destroy -t bya.clab.yml` (or `sudo containerlab destroy …` on Linux).
