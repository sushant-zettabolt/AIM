# Cluster Command Reference

Operational runbook for the real (kubeadm-based, non-kind, non-k3s) single-node
Kubernetes cluster this box runs, plus the AIM/llama.cpp + OpenWebUI Helm
deployment on top of it.

Facts specific to this setup, referenced throughout:
- Node name: `sushant` (control-plane, untainted so it also runs workloads)
- CRI socket: `unix:///run/containerd/containerd.sock` (Docker's containerd,
  reused — CRI plugin enabled, `SystemdCgroup = true`)
- Pod network: Flannel, `10.244.0.0/16`
- App namespace: `aim-demo-standalone`
- Helm release name: `smoketest`, chart at `deploy/helm/llm-chat/`
- kubeconfig: `/home/claude/.kube/config`, context `kubernetes-admin@kubernetes`
  (a `kind-aim-local` context may also still be present from the earlier kind
  setup — check with `kubectl config get-contexts` if `kubectl` seems to be
  talking to the wrong cluster)
- Commands prefixed `sudo` must be run by a sudo-capable user in their own
  terminal (password prompt required) — not something run non-interactively.

---

## 1. Cluster health & info

```bash
kubectl config get-contexts
```
Lists every cluster `kubectl` knows about and which one is active (`*`).
Useful first sanity check if commands seem to hit the wrong cluster.

```bash
kubectl config use-context kubernetes-admin@kubernetes
```
Switches `kubectl` to the real kubeadm cluster (as opposed to a leftover kind
context).

```bash
kubectl cluster-info
```
Prints the API server and CoreDNS endpoint URLs — confirms the control plane
is reachable at all.

```bash
kubectl get nodes -o wide
```
Node list with status (`Ready`/`NotReady`), roles, kube version, internal IP,
OS image, kernel version, container runtime version. Start here for "is the
cluster up" checks.

```bash
kubectl describe node sushant
```
Full detail on one node: conditions (`MemoryPressure`, `DiskPressure`,
`PIDPressure`, `Ready`), Allocatable vs Capacity (CPU/memory actually
schedulable, per the `kubeadmConfigPatches` reservation), and all pods
currently scheduled on it with their resource requests.

```bash
systemctl is-active containerd kubelet
```
Confirms the two host-level daemons the cluster depends on are actually
running (as opposed to `kubectl` just timing out).

```bash
kubectl get componentstatuses
```
(Deprecated API, but still often works on kubeadm clusters.) Quick
scheduler/controller-manager/etcd health check directly from the control
plane's point of view.

---

## 2. Pods, containers, status, logs

```bash
kubectl get pods -n aim-demo-standalone -o wide
```
Every pod in the app namespace: ready count, status, restarts, node, pod IP.
Add `-w` to watch it live as it changes.

```bash
kubectl get pods -A
```
Same, but cluster-wide — useful to check `kube-system`/`kube-flannel` are
healthy too, not just the app namespace.

```bash
kubectl get svc -n aim-demo-standalone
```
Services and their ClusterIPs/ports — what's reachable and how.

```bash
kubectl describe pod -n aim-demo-standalone -l app=llm-smoketest
```
Full detail on the llama.cpp AIM pod: image, resource requests/limits,
volumes, and — most useful when something's stuck — the **Events** section at
the bottom (scheduling failures, image pull progress/failures, probe
failures).

```bash
kubectl describe pod -n aim-demo-standalone -l app=aimsb-llm-chat-smoketest
```
Same, for the OpenWebUI pod.

```bash
kubectl get events -n aim-demo-standalone --sort-by='.lastTimestamp'
```
All events in the namespace in chronological order — the fastest way to see
what actually happened (scheduled → pulling → pulled → started → probe
results) without digging through `describe` output.

```bash
kubectl logs -n aim-demo-standalone -l app=llm-smoketest --tail=200
```
AIM engine logs: hardware detection (`cpu_detector`), profile selection
(`profile_registry`/`profile_selector`), the generated `llama-server` launch
command, then `llama-server`'s own model-load and per-request inference logs
(tokens/sec, timing).

```bash
kubectl logs -n aim-demo-standalone -l app=llm-smoketest -f
```
Same, following live as new chat requests come in.

```bash
kubectl logs -n aim-demo-standalone -l app=aimsb-llm-chat-smoketest -f
```
OpenWebUI's own logs (its Python/uvicorn backend).

```bash
kubectl logs -n aim-demo-standalone <pod-name> --previous
```
Logs from a pod's *previous* container instance — the one to reach for after
a restart/crash, since plain `logs` only shows the current instance.

```bash
kubectl exec -it -n aim-demo-standalone <pod-name> -- sh
```
Drop into a shell inside a running container for manual debugging (e.g.
checking `llama-server`'s process, disk usage under `/workload`).

```bash
kubectl top nodes
kubectl top pods -n aim-demo-standalone
```
Live CPU/memory usage. Requires `metrics-server` to be installed — **not**
installed on this cluster by default (kubeadm doesn't ship it); these will
error with "Metrics API not available" until it is.

```bash
kubectl port-forward -n aim-demo-standalone svc/aimsb-llm-chat-smoketest 8080:80
```
Opens OpenWebUI at `http://localhost:8080` (WSL2 forwards `localhost`
automatically to the Windows browser). Run in its own terminal — it blocks
until you Ctrl+C it.

---

## 3. Tear down the cluster

Run as a sudo-capable user. This resets the node back to a pre-`kubeadm init`
state — it does **not** uninstall `kubeadm`/`kubelet`/`kubectl`/`containerd`
themselves, only the cluster state they've built.

```bash
kubectl delete namespace aim-demo-standalone
```
Optional first step — deletes just the app (both Deployments, Services, and
anything else in that namespace) while leaving the cluster itself running.
Skip this if you're tearing down the whole cluster anyway (the namespace goes
with it).

```bash
sudo kubeadm reset -f --cri-socket=unix:///run/containerd/containerd.sock
```
The real teardown: stops the static-pod control plane (etcd, API server,
scheduler, controller-manager), wipes `/etc/kubernetes`, `/var/lib/etcd`, and
kubelet's local state. `-f` skips the interactive confirmation prompt.

```bash
sudo rm -rf /etc/cni/net.d
```
Removes leftover CNI (Flannel) network config — `kubeadm reset` doesn't
always clear this, and stale config here can break the *next* `kubeadm init`.

```bash
sudo ip link delete cni0 2>/dev/null || true
sudo ip link delete flannel.1 2>/dev/null || true
```
Removes the virtual network interfaces Flannel created. Harmless if they
don't exist (`|| true` swallows the "not found" error).

```bash
sudo iptables -F && sudo iptables -t nat -F && sudo iptables -t mangle -F && sudo iptables -X
```
Flushes the iptables rules kube-proxy installed (Service routing, NAT rules).
Without this, stale rules from the old cluster can interfere with the new
one's networking.

```bash
rm -rf /home/claude/.kube/config-kubeadm
```
Removes the now-invalid kubeadm kubeconfig on the `claude` side. (If you'd
merged it into `~/.kube/config` as the single active file, either delete that
too or manually strip the `kubernetes-admin@kubernetes` context with
`kubectl config delete-context` / `delete-cluster` / `delete-user`.)

---

## 4. Create the cluster and bring everything up

Assumes `containerd`/`kubeadm`/`kubelet`/`kubectl` are already installed and
containerd's CRI plugin is already enabled with `SystemdCgroup = true` (the
one-time host setup — not repeated here since package installs don't need
redoing after a `kubeadm reset`).

```bash
sudo systemctl is-active containerd
```
Sanity check before `kubeadm init` — confirms containerd survived the
teardown.

```bash
sudo kubeadm init --pod-network-cidr=10.244.0.0/16 --cri-socket=unix:///run/containerd/containerd.sock
```
Bootstraps the control plane: generates certs, starts etcd/API
server/scheduler/controller-manager as static pods, and prints a
`kubeadm join ...` line at the end (ignore it — not needed for single-node).
`--pod-network-cidr` must match whatever the CNI you install next expects
(`10.244.0.0/16` is Flannel's default).

```bash
mkdir -p /home/claude/.kube
sudo cp -i /etc/kubernetes/admin.conf /home/claude/.kube/config-kubeadm
sudo chown claude:claude /home/claude/.kube/config-kubeadm
```
Hands the cluster-admin credentials from `kubeadm init` (root-owned, under
`/etc/kubernetes`) to the unprivileged `claude` user as a separate
kubeconfig file. `kubectl`/`helm` need no further privilege after this —
they just read this file and talk to the API server over the network.

```bash
KUBECONFIG=/home/claude/.kube/config:/home/claude/.kube/config-kubeadm kubectl config view --merge --flatten > /tmp/merged
mv /tmp/merged /home/claude/.kube/config
kubectl config use-context kubernetes-admin@kubernetes
```
Merges the new context into the default kubeconfig (alongside any old `kind`
context) and switches to it, so plain `kubectl` (no `KUBECONFIG=` prefix)
talks to the new cluster.

```bash
kubectl apply -f https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml
```
Installs the Flannel CNI (VXLAN overlay) — without a CNI, nodes stay
`NotReady` and no pod networking works.

```bash
kubectl taint nodes --all node-role.kubernetes.io/control-plane-
```
Removes the default `NoSchedule` taint kubeadm puts on control-plane nodes.
Required for a single-node cluster, since this node has to run workloads too,
not just control-plane components.

```bash
kubectl get nodes
```
Confirms `sushant` shows `Ready` before moving on — Flannel takes ~10-30s to
finish setting up after `apply`.

### Deploy the AIM app

```bash
kubectl create namespace aim-demo-standalone
cd /home/claude/aim-build/deploy/helm/llm-chat
helm dependency build .
```
Creates the app namespace, then pulls the `aimchart-llm` subchart dependency
from its OCI registry (gitignored — must be re-fetched on every fresh
checkout/cluster).

```bash
helm template smoketest . -f values.yaml -f values.epyc-llamacpp.yaml | kubectl apply -f - -n aim-demo-standalone
```
Renders the chart (base values + the EPYC/llamacpp override) into plain
manifests and applies them directly — no `helm install`/release tracking, per
the chart's own recommendation, to avoid Secret-based release state that
doesn't play well with restricted clusters.

```bash
kubectl get pods -n aim-demo-standalone -w
```
Watch both pods come up. `llm-smoketest` (llama.cpp AIM) usually finishes
first; `aimsb-llm-chat-smoketest` (OpenWebUI) pulls a much larger image
(~1.7GB) and can take several minutes on a cold node with no cached layers.

```bash
kubectl port-forward -n aim-demo-standalone svc/aimsb-llm-chat-smoketest 8080:80
```
Once both pods show `1/1 Running`, open `http://localhost:8080`.
