# harvester-dev

Bring up a virtualized Harvester cluster on your local machine for development and testing.

## Architecture

### Components

The project provisions a Harvester cluster inside VMs on your host. Key components:

- **[config.yaml](config.yaml.sample)** — all tunables: image locations, IPs, MACs, VM counts, etc.
- **Terraform scripts** — VMs are defined with the [libvirt provider](https://github.com/dmacvicar/terraform-provider-libvirt); its templating also generates config files inside the Admin VM.
- **Bash scripts** — provisioning and daily-ops scripts. Ansible and similar tools are intentionally avoided to keep dependencies minimal.
- **Artifact server** — an nginx container that serves ISOs and images.

### VMs and Networks

Each libvirt network creates a Linux bridge with the same name on the host. Numbers in the diagram mark NIC indices (`0` = eth0, `1` = eth1, `2` = eth2).

```text
 +---------------+
 |    bridge     |
 | hvst-libvirt  |============== hvst-libvirt (192.168.123.0/24) ==============
 | 192.168.123.1 |              |                                     |
 +---------------+              |                                     |
                        +-------0--------+                   +--------0-------+
                        |   Admin Node   |                   |  Rancher Node  |
                        | eth0: libvirt  |                   | eth0: libvirt  |
                        | eth1: mgmt     |                   | eth1: mgmt     |
                        | eth2: data     |                   |                |
                        +----2------1----+                   +--------1-------+
 +---------------+           |      |                                 |
 |     bridge    |           |      |                                 |
 |   hvst-mgmt   |============== hvst-mgmt (10.0.10.0/24) =====================
 |    10.0.10.1  |           |       |            |            |          |
 +---------------+           |       |            |            |          |
                             |  +----0-----+ +----0-----+ +----0-----+
                             |  |  Node 1  | |  Node 2  | |  Node 3  |  ...
                             |  |10.0.10.11| |10.0.10.12| |10.0.10.13|
                             |  |          | |          | |          |
                             |  +----1-----+ +----1-----+ +----1-----+
 +---------------+           |       |            |            |          |
 |     bridge    |           |       |            |            |          |
 |   hvst-data   |============== hvst-data (10.0.11.0/24) =====================
 |    10.0.11.1  |              storage / nested VMs
 +---------------+
```

- **hvst-libvirt**
  - NAT; host libvirt dnsmasq assigns eth0 IPs for Admin/Rancher.
  - Admin node:
    - Runs dnsmasq on eth1/eth2 to serve mgmt/data IPs.
    - Serves PXE firmware and boot configs.
  - Rancher node is disabled by default; see [Rancher tasks](docs/index.md#rancher-tasks---manager-rancher-manager-and-guest-clusters) to provision it.
  - VM nodes are air-gapped by default; enable egress with `task op:admin-enable-egress`.

- **hvst-mgmt**
  - Harvester node VMs use this network as management network.
  - Non-subnet routes go through admin node.
  - The bridge IP (`10.0.10.1`) lets the host reach Harvester nodes directly. Most development tasks are accessed via this bridge. Node VMs can reach artifact server container via this IP.

- **hvst-data**
  - Storage and nested VM traffic.
  - Nested VMs get IPs from admin dnsmasq; egress follows the admin node setting.


## Prerequisites

Run the check script:

```bash
./scripts/check-prerequisites.sh
```

**KVM + libvirt** — install via your distro. Example on openSUSE Leap 15.6:

```bash
sudo zypper -n install -t pattern kvm_server kvm_tools
sudo systemctl enable --now libvirtd
groupadd libvirt
sudo usermod -a -G libvirt $USER  # then log out and back in
```

Set this if you're not root:

```bash
export LIBVIRT_DEFAULT_URI=qemu:///system
```

**Additional packages:**

```bash
sudo zypper -n install python3-pyaml go1.26
```

**Tools** (install via package manager or the `scripts/install-*` helpers):

- [task](https://taskfile.dev/)
- [terraform](https://developer.hashicorp.com/terraform)
- [yq](https://github.com/mikefarah/yq)


The helper scripts install tools in your `$HOME/bin/` directory.

## Setup

### 1. libvirt volume pool

The `default` pool is required for node disks:

```bash
sudo virsh pool-info default
```

If missing, create it:

```bash
sudo virsh pool-define-as --name default --type dir --target /var/lib/libvirt/images
sudo virsh pool-build default
sudo virsh pool-start default
sudo virsh pool-autostart default
```

> [!NOTE]
> AppArmor may block access if you mount non-default storage to `/var/lib/libvirt/images`. See the [troubleshooting guide](./docs/troubleshooting.md##permission-issue-when-not-using-the-default-image-path-varliblibvirtimages).

### 2. Firewall rules

```bash
sudo firewall-cmd --permanent --zone=public  --add-port=5951-5970/tcp  # VNC console
sudo firewall-cmd --reload
```

### 3. Clone the repo

```bash
git clone https://github.com/harvester/harvester-dev
cd harvester-dev
```

### 4. Configure and download images

```bash
cp config.yaml.sample config.yaml
task download-images
```

### 5. Plan networks

Generate random MAC addresses:

```bash
task generate-macs
```

Default subnets — check for conflicts with your existing network:

| Network | Bridge       | Host IP       | Subnet           |
|---------|--------------|---------------|------------------|
| NAT     | hvst-libvirt | 192.168.123.1 | 192.168.123.0/24 |
| MGMT    | hvst-mgmt    | 10.0.10.1     | 10.0.10.0/24     |
| DATA    | hvst-data    | 10.0.11.1     | 10.0.11.0/24     |

To override subnets:

```bash
task plan-networks -- --nat hvst-libvirt,192.168.123.0/24 --mgmt hvst-mgmt,10.0.20.0/24 --data hvst-data,10.0.21.0/24 config.yaml
```

Only `*.0/24` subnets are supported. For finer control, edit `config.yaml` directly.

For multiple deployments on one host, use a unique `provider.domain_prefix`,
unique bridge names, and nonoverlapping subnets for each deployment. Run network
planning before starting the artifact server; it updates the management IP in
artifact URLs while preserving their ports.

### 6. Bootstrap networks and start the artifact server

```bash
task networks-up
task artifacts-up
```

`networks-up` initializes Terraform and keys, then applies only the three libvirt
networks. This targeted apply is for bootstrapping; `task up` later applies the
full configuration. `artifacts-up` requires its configured IP to exist already
and does not provision infrastructure.

The artifact server (nginx) serves `./artifacts/isos` and `./artifacts/images`.
It binds to the IPv4 address and port in `artifact_server_url`, normally the
deployment's management bridge IP. For example, two deployments can use
`http://10.0.10.1:8787` and `http://10.0.20.1:8787` on the same host.
The local server supports HTTP only, with port 80 when omitted, and no URL path
other than `/`. Python 3 and the `ip` command are required to validate the address.

To use a different port, edit `artifact_server_url` and update existing artifact
URLs (or rerun the relevant ISO selectors). `PORT` is accepted only when it
matches the URL's port. There is no separate `artifact_server_port` setting.
After changing the address or port, rerun `task artifacts-up` and apply any
updated provisioning configuration before installing nodes.

For existing deployments, rerun `task artifacts-up` in each checkout to replace
containers that publish on all host interfaces before starting additional
deployments. The script replaces only the container named for that deployment's
prefix. When using external artifact hosting, skip local artifact startup.

### 7. Bring up the cluster

Download a Harvester ISO:

```bash
./artifacts/download-harvester-iso.sh           # master
./artifacts/download-harvester-iso.sh v1.8.1    # specific release
```

Select the ISO to use:

```bash
./artifacts/select-install-iso.sh
```

Edit `config.yaml` as needed (node count, CPU, memory), then start the cluster:

```bash
task up
```

> [!NOTE]
> To use external artifacts instead of the local artifact server, set `.admin.egress_enabled: true` and point these fields to external URLs:
> ```
> harvester_iso_url:
> harvester_kernel_url:
> harvester_ramdisk_url:
> harvester_rootfs_url:
> ```
> This enables egress on the admin node, so the environment is no longer air-gapped.

## Usage

**Kubernetes access** — a `kubeconfig` file is created in the project root after the cluster is up.

**SSH:**

```bash
task ssh            # admin node
task ssh -- node1   # specific node
```

SSH config is at `./state/ssh_config`.

**VNC** — nodes listen on `0.0.0.0:5951+`. Connect to `<host-ip>:5951`, `:5952`, etc.

**Internet** — nodes route through the admin node, which does not forward traffic by default.

```bash
task op:admin-enable-egress   # enable forwarding
task op:admin-disable-egress  # disable forwarding
```

## Tear down

```bash
task clean
```

Destroys and undefines all libvirt domains, volumes, and networks.

## More

See the [full task list](./docs/index.md) for additional commands.
