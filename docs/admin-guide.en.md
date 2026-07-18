# Administering the lab server (`iipaserver`)

**Draft — English. To be translated into Russian.**

This guide covers the everyday administration of the lab's server after its move
from bare-metal Ubuntu to Proxmox VE. It assumes no prior Proxmox experience.
Almost everything here is done by clicking in a web page; where a terminal is
genuinely required, the exact command is given.

---

## 1. The mental model

The most important idea: **the machine you used to log into is now a guest
inside a larger host.**

- The **host** (`iipaserver`) is the physical server. It runs Proxmox VE and
  does nothing else. Its job is to hand out CPU, memory and GPUs to virtual
  machines. **Do not install research software here.**
- A **virtual machine (VM)** is a complete, self-contained computer running
  inside the host. The old Ubuntu system is now VM number **100**, named
  `migrated-ubuntu`. It is the same system you had before, with the same files.
- Each VM has a **VMID**, a permanent number. VMIDs are how Proxmox refers to
  VMs everywhere. Ours are **100** (the real workload) and **101** (`sample`, a
  blank scratch VM that is safe to break).

Why this change was worth it: the host can now be modified, upgraded and have
dependencies installed without touching the research environment, and the
research environment can be snapshotted, backed up, and restored.

---

## 2. Getting in

### The web interface (the main tool)

Open **<https://192.168.71.113:8006>** in a browser.

- Your browser will warn that the certificate is not trusted. This is expected —
  the server uses a self-signed certificate. Accept and continue.
- Log in with user **`root`**, realm **`Linux PAM standard authentication`**.

The left panel is a tree: *Datacenter* → the node `iipaserver` → the VMs (100,
101). Click a VM to manage it.

This address is only reachable from the lab network. To open the web interface
from a computer on the outside internet, see **§6 → "Reaching the Proxmox web
interface from an outside computer"**.

### Reaching the Ubuntu VM (what researchers use)

The Ubuntu VM has its own address on the lab network, **192.168.71.222**. Reach
it directly:

```
ssh <username>@192.168.71.222
```

This is the same Ubuntu, with the same accounts, as before the migration. For
most lab users this is the only thing they need to know, and nothing else in
this guide affects them.

### The host's own shell

Only needed for the few tasks marked "terminal" below.

```
ssh root@192.168.71.113
```

You can also get a shell from the web interface: select `iipaserver` in the
tree, then **Shell**. That avoids needing an SSH client at all.

---

## 3. Everyday VM operation

Select the VM in the tree. The buttons are at the top right.

| Action | What it does | When |
|---|---|---|
| **Start** | Powers the VM on. | After a shutdown. |
| **Shutdown** | Asks the OS to shut down cleanly. | Normal way to stop. |
| **Reboot** | Clean restart. | After kernel/driver updates in the guest. |
| **Stop** | Cuts the power instantly. | **Last resort only** — can corrupt data, exactly like pulling the plug. |
| **Console** | A screen and keyboard attached to the VM. | When the network is broken and SSH won't work. |

Prefer **Shutdown** over **Stop**. Use **Stop** only if Shutdown has already
failed and the VM is unresponsive.

**Summary** (per VM) shows CPU, memory and disk graphs — the first place to look
when someone reports the server is slow.

### Automatic start after a power cut

VM 100 has **Start at boot** enabled, so it comes back by itself when the host
reboots. VM 101 does not. You can see and change this under *VM → Options →
Start at boot*.

---

## 4. Moving GPUs between VMs

The server has **three NVIDIA Tesla V100 (32 GB)** cards. In Proxmox they are
registered as *mapped resources* named **`gpu0`, `gpu1`, `gpu2`** — always use
these names, never raw PCI addresses.

A GPU can be used by **only one running VM at a time**. Normally all three
belong to VM 100.

### The rule that surprises everyone

**Only a *running* VM actually owns a GPU.** A stopped VM's configuration can
still *claim* a GPU that another VM is using. Proxmox permits this and only
complains when you try to start the second VM — which then fails. So a
configuration that looks fine can be broken in a way you only discover at start
time.

The tool below is designed around exactly this problem.

### `pvepci` — the supported way to move GPUs

`pvepci` is installed on the host (this is a **terminal** task; there is no web
button for it). It moves GPUs between VMs in one command, restarts only the VMs
that must be restarted, and checks first whether anybody is using the GPUs.

**Look before you touch — this changes nothing:**

```
pvepci status
```

```
node  iipaserver

devices
  MAPPING  PATH          GROUP  HELD BY                   ALSO CLAIMED
  gpu0     0000:01:00.0  49     VM 100 (migrated-ubuntu)
  gpu1     0000:81:00.0  13     VM 100 (migrated-ubuntu)
  gpu2     0000:c1:00.0  2      VM 100 (migrated-ubuntu)

vms
  VMID  NAME             STATE    DEVICES
  100   migrated-ubuntu  running  gpu0, gpu1, gpu2
  101   sample           stopped  -
```

An entry in the **ALSO CLAIMED** column is the stale-claim problem described
above. It is a warning, not damage.

**Is anyone working right now?**

```
pvepci check
```

This reports each running VM as `IDLE` or `BUSY`, based on who is logged in,
what long-running processes exist, how long the VM has been up, and GPU
utilisation *sampled over 30 seconds*.

> Note: GPU **memory** is deliberately ignored. The language-model service holds
> about 10 GB on every card permanently, even when doing nothing, so memory use
> is not evidence that anyone is working.

**Ready-made layouts** are defined in the config file and listed with:

```
pvepci profiles
```

Currently:

- **`all-to-ubuntu`** — all three GPUs to VM 100. *This is the normal state.*
- **`split-2-1`** — two GPUs to VM 100, one to VM 101.
- **`all-to-sample`** — all three to VM 101.

**Always dry-run first.** This shows exactly what would happen and changes
nothing:

```
pvepci apply --profile split-2-1 --dry-run
```

**Then apply it:**

```
pvepci apply --profile split-2-1
```

It will show the plan, run the safety checks, announce the shutdown inside the
affected VMs, wait a grace period so a logged-in user can object, then stop only
the VMs that must be stopped, rewrite their configuration, and restart exactly
those that were running before. A VM that was already stopped stays stopped.

Useful flags:

| Flag | Effect |
|---|---|
| `--dry-run` / `-n` | Show the plan, change nothing. Use this first, always. |
| `--force` | Proceed even though the safety checks object. |
| `--grace SECS` | Change how long users get to object. |
| `--set 100=gpu:2 101=gpu:1` | A one-off layout, without a profile. Cannot be combined with `--profile`. |

If the safety checks report `BUSY`, **find out who is working before using
`--force`.** The checks are warnings, not locks — they will not stop you, they
only tell you.

### Adding a new layout

Profiles live in **`/etc/pve/pvepci.yaml`** on the host. To add one, edit that
file by hand and add an entry under `profiles:`, for example:

```yaml
profiles:
  my-layout:
    100: { gpu: 1 }
    101: { gpu: 2 }
```

Then confirm it with `pvepci profiles` and `pvepci apply --profile my-layout
--dry-run`.

### Consequence for users: the GPU count can change

The language-model service on VM 100 (the docker container `qwen`) is configured
for **exactly three GPUs**. If you move a GPU away, that container will not
start again until all three are back — it fails with `device error: 2: unknown
device`. It restarts by itself, correctly, as soon as three GPUs are restored.

This is accepted behaviour, not a fault. **Lab users should be told that the
number of visible GPUs can change**, and that software should not assume three
cards are always present.

---

## 5. Snapshots and backups

> **This is the most important section of the guide.** At the time of writing,
> **no backups and no snapshots exist**, and VM 100 holds ~320 GB of research
> data. Section 9 covers setting this up properly.

### Snapshots — before you do anything risky

A snapshot records the VM's exact state so you can jump back to it.

*VM → Snapshots → Take Snapshot.* Give it a name and a description. To go back:
select it and press **Rollback**.

**Take a snapshot before**: upgrading anything inside the VM, installing GPU
drivers, or any change you are not certain about. It takes seconds and has saved
more systems than any other Proxmox feature.

Two cautions:

- A snapshot is **not a backup**. It lives on the same disks. If a disk dies,
  the snapshot dies with it.
- Snapshots grow over time and consume space. Delete them when the change has
  proven good. Do not keep them for months.

### Backups

*VM → Backup → Backup now*, with mode **Snapshot** (the VM keeps running).
Restoring is the **Restore** button on the same screen.

Note the constraint: VM 100's disk is ~894 GB, and the host's `local` storage
holds only ~40 GB. **A full backup of VM 100 does not fit there.** It must go to
the 4 TB pool or to external storage — see section 9.

Reference: <https://pve.proxmox.com/wiki/Backup_and_Restore>

---

## 6. How the network is put together

The physical server has one lab-network address of its own — **192.168.71.113**
on the bridge `vmbr0` — but the lab network (`192.168.71.0/24`, gateway
`192.168.71.1`) has room for more. A VM joins the network in one of two ways,
depending on whether it has been given a dedicated address.

### A VM with its own dedicated address (the Ubuntu VM)

The Ubuntu research VM (VM 100) has a dedicated lab-network address,
**192.168.71.222**. A VM like this goes straight onto the lab network:

- Attach its network card to the bridge **`vmbr0`** (*VM → Hardware →* the
  network device *→ Bridge* `vmbr0`).
- Set the address **192.168.71.222** (gateway 192.168.71.1) in the VM's own
  network configuration.

It then sits **directly** on the lab network, like the host itself: reached at
`192.168.71.222` (e.g. `ssh <user>@192.168.71.222`) with no port-forward on the
host involved. It is **not** behind the host's NAT, so the port-forwarding rules
below do not apply to it.

### A VM or container without a dedicated address

For anything ad-hoc — a scratch VM like 101, a throwaway container — attach it
instead to the **internal** virtual network (Proxmox SDN, zone `internal`,
network `vnet0`, range **192.168.1.0/24**, gateway **192.168.1.1**). There:

- **SNAT is on**, so it can reach the internet outbound with no extra work.
- Inbound, nothing is reachable from outside until a port is explicitly
  forwarded on the host — see *Port forwarding* below.

### Port forwarding

A VM on the **internal** network is reachable from outside the host only through
a port-forward in `nftables`. (A VM with a dedicated address, like Ubuntu, needs
none of this — it is reached directly.)

The rules are **not** in `/etc/nftables.conf` — that file is only a skeleton.
The real rules live in **`/etc/nftables.conf.d/portforward.nft`** (terminal
task). For example, to expose a web service on VM 101 (at 192.168.1.11) as port
8080 on the host:

```
table ip portfwd {
    chain prerouting {
        type nat hook prerouting priority dstnat; policy accept;
        iifname "vmbr0" tcp dport 8080 dnat to 192.168.1.11:80
    }
}
```

Add a further `dnat` line in the same shape for each additional service. Then
apply and verify:

```
systemctl reload nftables
nft list ruleset
```

Two warnings:

- **Anything you forward becomes reachable from the lab network.** Forward only
  what is meant to be public.
- A syntax error means `nftables` fails to load. **Check with `nft list ruleset`
  after every change**, and keep a session open until you have confirmed you can
  still get in.

Reference: <https://pve.proxmox.com/wiki/Software-Defined_Network>

### Reaching the lab from the internet (through `kron.botik.ru`)

There are **two separate firewalls**, and it matters which one a rule goes on:

- **The host's own firewall** (the `nftables` rules just above) only moves
  traffic between the internal network and the VMs. It cannot make anything
  reachable from the public internet.
- **`kron.botik.ru`** is the organisation's internet-facing gateway — the same
  machine you jump through with `ssh -J llm_test2@kron.botik.ru …`. Only a rule
  **on kron** can expose a service to the outside world, and those rules are
  applied by Botik's administrators, not on this server.

#### The existing LLM forward — leave it in place

The language-model team already had a forward added on kron so that
`http://kron.botik.ru:8080`, **restricted to the single source IP
141.105.66.202**, reaches their service. On kron this is a DNAT to
`192.168.71.113:8080` with matching `FORWARD`/`MASQUERADE` rules. **Do not
remove it.** (Now that VM 100 has its own address, 192.168.71.222, that team
will likely want the destination changed from `192.168.71.113:8080` to
`192.168.71.222:8080` so it reaches the VM directly — that is theirs to
coordinate.)

#### Reaching the Proxmox web interface from an outside computer

The web UI lives on the host at `192.168.71.113:8006`. To use it from a machine
on another provider — whose IP is often not known in advance — there are two
ways.

**Recommended: an SSH tunnel. No firewall changes, works from any computer.**
Since you can already SSH to kron, forward the web UI over that connection:

```
ssh -L 8006:192.168.71.113:8006 llm_test2@kron.botik.ru
```

Leave it running and open **<https://localhost:8006>** in the browser on your
laptop. This needs no rule on kron, exposes nothing to the internet, and works
from *any* source IP — exactly the "a different computer each time" case. This
is the right answer in almost every situation.

**Alternative: a DNAT on kron, like the LLM team's.** Only if a browser must
reach the server with no SSH client available. Ask Botik's administrators to
add, **alongside** the existing 8080 rules (replace `<ALLOWED_IP>` with the
specific address you will connect from):

```
iptables -t nat -A PREROUTING  -p tcp -s <ALLOWED_IP> --dport 8006 -j DNAT --to-destination 192.168.71.113:8006
iptables        -A FORWARD     -p tcp -s <ALLOWED_IP> -d 192.168.71.113 --dport 8006 -j ACCEPT
iptables        -A FORWARD     -p tcp                 -d 192.168.71.113 --dport 8006 -j DROP
iptables -t nat -A POSTROUTING -p tcp                 -d 192.168.71.113 --dport 8006 -j MASQUERADE
```

> **Serious warning.** Port 8006 is the Proxmox **root** login. Exposing it to
> the internet puts the whole server one password away from anyone who can reach
> that port. **Always keep the `-s <ALLOWED_IP>` restriction** — never open 8006
> to all sources — and remember you would have to add each new outside IP by
> hand. That fragility is exactly why the SSH tunnel above is the better tool.

---

## 7. Creating a new VM

*Top right → Create VM.* The wizard, in order:

1. **General** — pick a free VMID (102 next) and a name.
2. **OS** — choose an installer ISO. Upload ISOs under *local → ISO Images →
   Upload*.
3. **System** — leave the defaults; set **QEMU Guest Agent** on (it lets the
   host talk to the VM properly).
4. **Disks** — put the disk on **`nvme-thin`** (fast) or **`four-tb-thin`**
   (large). Not `local`, which is small and meant for ISOs and backups.
5. **CPU/Memory** — be conservative. The host has 96 threads and 270 GB, but VM
   100 is already assigned 200 GB.
6. **Network** — set the bridge to **`vnet0`** so it lands on the internal
   network and gets an address automatically.

After installing the OS, install the guest agent inside it (`apt install
qemu-guest-agent`), and add a port-forward from section 6 if it needs to be
reachable.

To give it GPUs, add it to `/etc/pve/pvepci.yaml` and use `pvepci` — do not
attach GPUs by hand in the web interface.

Storage available:

| Storage | Size | Use for |
|---|---|---|
| `nvme-thin` | ~910 GB | fast VM disks |
| `four-tb-thin` | ~3.7 TB | large VM disks, data |
| `local` | ~40 GB | ISOs, templates — **small** |

---

## 8. Resizing a VM's disk

A disk can be **grown but not shrunk**. The job has two halves: enlarge the
virtual disk in Proxmox, then grow the filesystem inside the guest so the new
space is actually usable. Doing only the first half is the usual "I resized it
but the disk is still full" surprise.

### 1. Enlarge the disk (web interface)

*VM → Hardware.* Select the disk (e.g. `scsi0` or `virtio0`), then **Disk Action
→ Resize**. Enter the amount to **add**, not the new total — "50" adds 50 GiB.
This is safe and can be done while the VM runs; because it only ever grows,
there is no risk of cutting off data.

Check the target storage has room first (*Node → the storage → Summary*, or the
table in section 7). `nvme-thin` and `four-tb-thin` are the roomy ones; `local`
is not.

### 2. Grow the filesystem (inside the guest — terminal)

The guest now sees a bigger disk, but its partition and filesystem are still the
old size. Inside the VM:

```
lsblk                     # find the disk and partition, e.g. /dev/sda, /dev/sda1
```

For a plain partition with ext4 (the common case):

```
sudo growpart /dev/sda 1  # grow partition 1 to fill the disk (note the space before 1)
sudo resize2fs /dev/sda1  # grow the ext4 filesystem to fill the partition
```

For XFS, the final step is `sudo xfs_growfs /` instead of `resize2fs`.

If the VM uses **LVM**, grow the physical volume and logical volume instead:

```
sudo growpart /dev/sda 3
sudo pvresize /dev/sda3
sudo lvextend -l +100%FREE /dev/mapper/<vg>-<lv>
sudo resize2fs /dev/mapper/<vg>-<lv>   # or: sudo xfs_growfs / for XFS
```

Confirm with `df -h` that the mount point is now larger.

> `growpart` lives in the `cloud-guest-utils` package (`apt install
> cloud-guest-utils`) if it is missing. The enlarge step in Proxmox is safe, but
> partition edits inside the guest are worth a safety net — take a snapshot
> (section 5) before resizing a root disk you cannot afford to lose.

---

## 9. Recommended practice (not yet done)

These are not set up. They are listed in the order they matter.

1. **Configure a scheduled backup.** *Datacenter → Backup → Add.* Nothing
   protects VM 100's ~320 GB today. Remember it will not fit on `local`; target
   `four-tb-thin` or, better, external storage — a backup on the same machine
   does not survive the machine.
2. **Stop using `root` for everyday work.** Today `root@pam` is the only
   account. Create a personal account under *Datacenter → Permissions → Users*
   (realm "Proxmox VE authentication server") and grant it a role such as
   `PVEVMAdmin` on the VMs. This gives each person their own login and an audit
   trail. See <https://pve.proxmox.com/wiki/User_Management>.
3. **Test a restore.** A backup that has never been restored is not yet known to
   work. Restore into a new VMID and confirm it boots.
4. **Keep the host updated.** *Node → Updates → Refresh*, then **Upgrade**;
   reboot when the kernel changes. Note the host uses the free
   `pve-no-subscription` repository, which is fine for this server. The
   subscription warning at login is cosmetic and can be ignored.
5. **Note about `pve-nag-buster`.** A third-party script is installed that
   suppresses the subscription pop-up. It is harmless, but it hand-edits
   Proxmox's repository files, so if updates ever behave strangely, suspect it
   first.

---

## 10. If something goes wrong

| Symptom | Likely cause and first step |
|---|---|
| Can't reach Ubuntu at 192.168.71.222 | Is VM 100 running? Check the web UI. Then confirm its network card is on `vmbr0` and the address is set in the VM's network config. |
| VM won't start, GPU error | Two VMs claim the same GPU. Run `pvepci status` and look at **ALSO CLAIMED**. |
| The LLM service is gone after a GPU move | Expected if VM 100 has fewer than three GPUs. Restore all three; the container returns by itself in under a minute. |
| VM is unresponsive | Try **Console** first. **Shutdown**, and only then **Stop**. |
| Out of disk space | *Node → Disks / storage view*. Old snapshots are a common cause. |
| Locked up after a failed operation | The VM may hold a lock: `qm unlock <VMID>` on the host. |

**The general rule: look before you change.** `pvepci status`, `pvepci check`,
and `--dry-run` all change nothing and cost nothing. They have caught real
problems that would not have been noticed otherwise.

---

## 11. Reference

- Proxmox VE admin guide: <https://pve.proxmox.com/pve-docs/>
- Backup and restore: <https://pve.proxmox.com/wiki/Backup_and_Restore>
- User management: <https://pve.proxmox.com/wiki/User_Management>
- SDN: <https://pve.proxmox.com/wiki/Software-Defined_Network>
- PCI passthrough background: <https://pve.proxmox.com/wiki/PCI_Passthrough>

### The facts of this server, in one place

| | |
|---|---|
| Host address | 192.168.71.113 (web UI on port 8006) |
| Node name | `iipaserver` |
| Proxmox version | 9.2.4 |
| Hardware | AMD EPYC 7642 (48 cores / 96 threads), 270 GB RAM |
| GPUs | 3 × Tesla V100-SXM3-32GB → `gpu0`, `gpu1`, `gpu2` |
| VM 100 `migrated-ubuntu` | the old system; Ubuntu 20.04; 192.168.71.222 on `vmbr0`; auto-starts |
| VM 101 `sample` | blank scratch VM; safe to break |
| Reaching VM 100 | `ssh <user>@192.168.71.222` (dedicated lab address on `vmbr0`) |
| Internal network | 192.168.1.0/24, gateway 192.168.1.1, `vnet0` (ad-hoc VMs/containers) |
| Port forwarding (host) | `/etc/nftables.conf.d/portforward.nft` |
| Internet gateway | `kron.botik.ru` (org-managed; forwards to 192.168.71.113) |
| Web UI from outside | `ssh -L 8006:192.168.71.113:8006 llm_test2@kron.botik.ru` → <https://localhost:8006> |
| GPU tool config | `/etc/pve/pvepci.yaml` |
