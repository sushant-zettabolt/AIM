<!--
Copyright © Advanced Micro Devices, Inc., or its affiliates.

SPDX-License-Identifier: MIT
-->

# Kubernetes, from zero, using our own cluster as the example

This is written for someone who has never touched Kubernetes. Instead of explaining
concepts abstractly, every section uses the **real, live cluster** running this
project's vLLM/llama.cpp AIM deployment on `volcano-a942-host` as the worked
example — real IPs, real commands, real output pulled off the box, not toy
examples. By the end you should understand not just "what is a Service" in the
abstract, but exactly what happens, hop by hop, when you `curl` this system.

---

## 1. The absolute basics: container → Pod → Node → Cluster

**Container.** You probably already know this one: a single process (or small
group of tightly coupled processes) packaged with its own filesystem, isolated
from the host, via `containerd`/Docker. Nothing Kubernetes-specific yet.

**Pod.** Kubernetes never schedules a bare container — it schedules a **Pod**, which
is one or more containers that always live and die together, share one network
namespace (one IP address), and can talk to each other over `localhost`. In this
project, every Pod we run has exactly one container (`aim-llm`) running the
actual vLLM or llama.cpp process, plus sometimes a short-lived `model-cache-init`
container that runs once, before the main one starts, and exits.

**Node.** A physical or virtual machine that actually runs Pods. Our cluster has
exactly **one** Node: `volcano-a942-host`, a real AMD EPYC box.

**Cluster.** The whole system: one (or more) machines running the Kubernetes
control plane (the "brain" — API server, scheduler, controller manager) plus one
or more Nodes running the actual workloads. Confirmed on our box:

```
$ kubectl get nodes -o wide
NAME                STATUS   ROLES           VERSION        INTERNAL-IP
volcano-a942-host   Ready    control-plane   v1.36.4+k3s1   10.194.198.72
```

Notice the ROLE says `control-plane` — on this cluster, the one Node is *also*
running the control plane. That's normal for a small/dev cluster and is exactly
what **k3s** (see next section) is built for.

---

## 2. What is k3s, specifically (vs. "Kubernetes" in general)

"Kubernetes" is a specification/project with many independent components
(API server, scheduler, controller-manager, etcd datastore, kube-proxy, a
container runtime, a network plugin, DNS...). A full production cluster usually
runs each of these as separate processes/Pods, often across many machines.

**k3s** is a single, small binary from Rancher/SUSE that bundles almost all of
those components into **one process**: `/usr/local/bin/k3s server`. On our box,
that one process *is* the API server, the scheduler, the controller-manager, an
embedded lightweight datastore (SQLite, not etcd), the kubelet (the per-Node
agent that actually starts/stops containers), and — important for later sections
— **kube-proxy**, all running inside a single OS process. It also bundles a
default CNI (Flannel, for Pod networking), a default Ingress controller
(Traefik — §7), and a default local storage provisioner.

Why this matters for reading this document: whenever we say "kube-proxy did X"
or "the API server did Y," on a normal cluster those would be separate Pods you
could `kubectl get pods -n kube-system` and see individually. On our cluster,
some of those (kube-proxy in particular) are invisible as Pods — they're just
code running inside the one `k3s server` process. Others (Traefik,
metrics-server, CoreDNS) *are* regular Pods you can see and inspect normally,
because k3s installs them as ordinary Kubernetes workloads on top of itself.

---

## 3. Namespaces — just a folder, not a security boundary by default

A `Namespace` is a way to partition names within one cluster — two objects can
share a name if they're in different namespaces. Everything we built lives in
`aim-demo-standalone`:

```
kubectl get pods -n aim-demo-standalone
```

Think of it like a subdirectory. It does not, by itself, provide network
isolation or resource isolation — Pods in different namespaces can talk to each
other freely unless something else (a NetworkPolicy) restricts it. Nothing in
this project relies on namespace isolation for anything; it's used purely for
organization (keeping our stuff separate from `kube-system`'s stuff).

---

## 4. Deployment — "keep N copies of this Pod template running"

You almost never create a Pod directly. Instead you create a `Deployment`, which
is a declaration: "run N copies of this exact Pod template, and if any of them
die, restart them." A `ReplicaSet` (which you rarely interact with directly) is
the object that actually watches the Pod count and reconciles it; the
Deployment mostly exists to manage rolling updates of the ReplicaSet.

Here's where our project gets specific. Look at
[`deployment.yml`](../aimchart-llm/templates/deployment.yml) — it actually
defines **two different shapes** of Deployment, and the reason why is the whole
point of this system:

### 4a. The simple shape — one Deployment, N replicas

```yaml
spec:
  replicas: {{ .Values.replicas | default 1 }}
  template:
    spec:
      containers:
        - name: aim-llm
          command: ["taskset", "-c", {{ .Values.cpuAffinity | quote }}, "./entrypoint.py"]
```

This is the textbook pattern: one Deployment, `replicas: N`, Kubernetes clones
the *same* Pod template N times. Fine for identical, interchangeable replicas.

### 4b. The problem: we don't want identical replicas

We wanted **10 pinned instances, each locked to a different set of physical
CPU cores** (instance A on cores 96-103, instance B on 104-111, etc. — see §8).
But every replica of one Deployment **shares the exact same Pod template** — you
cannot tell Kubernetes "replica #3 gets `command: [taskset -c 112-119 ...]` but
replica #4 gets a different one." A Deployment's replicas are clones, not
variations.

So `deployment.yml` has a *second* template, `aimchart-llm.deployment.instance`,
and a values-driven loop:

```yaml
{{- if .Values.instances }}
{{- range $inst := .Values.instances }}
{{- include "aimchart-llm.deployment.instance" $ictx }}
---
{{- end }}
{{- end }}
```

Given a values file like
[`values.epyc-vllm-10instances.yaml`](../values.epyc-vllm-10instances.yaml):

```yaml
llm:
  instances:
    - name: a
      cpuAffinity: "96-103"
    - name: b
      cpuAffinity: "104-111"
    # ... h more entries ...
```

this loop stamps out **10 separate Deployment objects** — `llm-vllm-pinned10-a`
through `-j` — each with `replicas: 1` and its own `cpuAffinity` baked into its
own `command:` override. Ten Deployments instead of one Deployment×10, purely
because that's the only way to give each Pod a genuinely different container
command. Confirmed live:

```
$ kubectl get deploy -n aim-demo-standalone
NAME                            READY
llm-vllm-pinned10-a             1/1
llm-vllm-pinned10-b             1/1
...
llm-vllm-pinned10-j             1/1
```

Each of these 10 Deployments has its own selector (`app: llm-vllm-pinned10`,
`instance: "a"`/`"b"`/...) so Kubernetes doesn't reject them as overlapping —
but they all share the *same* `app` label. That's deliberate, and it's the hook
that makes §6 (Services) work: one Service can select all 10 at once.

---

## 5. How the CPU pinning itself actually works (not a Kubernetes feature)

This is worth being explicit about: **Kubernetes' own CPU accounting
(`resources.requests`/`limits`) is not what pins a process to specific cores in
this system.** Kubernetes does have a real feature for that — the kubelet's
"static" CPU Manager policy, which *can* give a Pod an exclusive set of cores —
but we don't use it. Instead:

```yaml
command: ["taskset", "-c", "96-103", "./entrypoint.py"]
```

`taskset` is an ordinary Linux utility that calls `sched_setaffinity()` on
itself before executing the real program, restricting which CPU cores the
Linux scheduler is allowed to run it on. This has nothing to do with
Kubernetes — it would work identically if you ran this command directly on a
plain Linux box, no cluster involved. Kubernetes' only role here is: it's the
mechanism that got this exact `command:` array into a specific container on a
specific Node.

The one subtlety: `taskset` sets the affinity mask on the process it launches
(`./entrypoint.py`, a Python wrapper). Does that mask survive once
`entrypoint.py` hands off to the real engine (`llama-server` or vLLM's
`api_server`)? Yes — because `entrypoint.py` doesn't `fork()` a child process
for the engine, it calls `os.execv()`, which **replaces the current process's
program code in place while keeping the same PID and the same CPU affinity
mask**. So the mask taskset set on the wrapper process is still in effect on
the real inference engine after the handoff. No application code was changed
to make this work — it's a property of how `execv` behaves that the deployment
was arranged to take advantage of.

One extra wrinkle specific to vLLM: `taskset` only restricts *CPU* affinity, it
never touches *NUMA memory-node* affinity. vLLM has its own internal logic
(`VLLM_CPU_OMP_THREADS_BIND=auto`) that tries to auto-detect which NUMA node an
instance "belongs to" from the raw (unrestricted) memory-affinity list — which,
without help, always picks node 0, wrongly, for every instance we pinned onto
node 1. The fix was another plain environment variable, `CPU_VISIBLE_MEMORY_NODES`,
set per instance to its real pinned node — a good concrete example of "the
cluster orchestration was completely correct; the *application* had its own
internal assumption that needed a matching hint." See
[`values.epyc-vllm-10instances.yaml`](../values.epyc-vllm-10instances.yaml)'s
comments for the full trace.

---

## 6. Service — a stable virtual IP over a changing set of Pods

Pods are disposable — they get rescheduled, restarted, get new IPs. You can't
hand out a Pod's IP address to clients. A `Service` solves this: it's a stable
virtual IP that Kubernetes maintains, which always forwards to *whichever* Pods
currently match a label selector.

Our [`service.yml`](../aimchart-llm/templates/service.yml) is about as simple
as it gets:

```yaml
spec:
  type: ClusterIP
  ports:
    - port: 80
      targetPort: 8000
  selector:
    app: llm-vllm-pinned10
```

```
$ kubectl get svc -n aim-demo-standalone
NAME                 TYPE        CLUSTER-IP      PORT(S)
llm-vllm-pinned10    ClusterIP   10.43.194.231   80/TCP
```

Because all 10 of our per-instance Deployments' Pods share the label
`app: llm-vllm-pinned10` (see §4b), this **one** Service automatically fans out
over **all 10** Pods — Kubernetes tracks this match live via an `EndpointSlice`
object, one entry per matching, *Ready* Pod (a Pod that hasn't passed its
readinessProbe, e.g. still loading model weights, is invisible to the Service).

**Important nuance directly disproving a natural assumption:** a Service is
*neither* "one Service per Pod" *nor* "one Service per Node." It's a
label-selector grouping, completely decoupled from both — it can point at 1
Pod, 10 Pods, or 0 Pods, on any Node, and doesn't care.

### 6a. What actually forwards the traffic: kube-proxy

The Service's `10.43.194.231` IP is **virtual** — no process anywhere is
actually listening on it. Something has to rewrite packets addressed to it into
packets addressed to a real Pod IP. That something is **kube-proxy** (on our
cluster: code running inside the one `k3s server` process, §2), and its job is
to continuously watch Services/EndpointSlices and program the Node's own
kernel-level packet filter to do that rewriting.

We proved exactly how, on this box, by spinning up a temporary privileged Pod
and reading the real kernel rules off the Node:

```
-A KUBE-SVC-O45IHAHWHMIT3MCD ... --probability 0.10000000009 -j KUBE-SEP-... → 10.42.0.72:8000
-A KUBE-SVC-O45IHAHWHMIT3MCD ... --probability 0.11111111101 -j KUBE-SEP-... → 10.42.0.73:8000
-A KUBE-SVC-O45IHAHWHMIT3MCD ... --probability 0.12500000000 -j KUBE-SEP-... → 10.42.0.74:8000
   ... (7 more, probability shrinking each time) ...
-A KUBE-SVC-O45IHAHWHMIT3MCD                                  -j KUBE-SEP-... → 10.42.0.81:8000  (no probability = last resort, always matches)
```

This is `iptables` (specifically its `nf_tables`-backed mode, confirmed via
`iptables --version` → `nf_tables`). Read top to bottom: each rule has an
independent chance (`p`) of matching a given packet; the sequence
`1/10, 1/9, 1/8, ..., 1/1` is arranged so that the *net* effect, after falling
through possibly several non-matches, is a uniform 1-in-10 chance of landing on
any given Pod. Each matched rule jumps to a `KUBE-SEP-*` chain that does one
thing:

```
-A KUBE-SEP-MNEGI7MMI7JSECJR -j DNAT --to-destination 10.42.0.72:8000
```

Plain Destination NAT: rewrite the packet's destination address in place from
the Service's virtual IP to a real Pod IP. That's the entire mechanism. There
is no named "algorithm" here (no round-robin, no least-connections) — it's a
chain of independent coin-flips (`statistic --mode random`) tuned to average
out to uniform-random. This is chosen *per new TCP connection*, not per HTTP
request — everything on one connection goes to the same Pod.

### 6b. "Textbook" scheduling algorithms — where those actually live

If you've heard Kubernetes load balancing can use algorithms like round-robin,
weighted round-robin, least-connections, source-hashing — those are real, but
they belong to **IPVS**, an optional *alternate* kube-proxy backend, not the
default. IPVS is a genuine in-kernel Layer-4 load balancer (from the older LVS —
"Linux Virtual Server" — project, which predates Kubernetes by well over a
decade). It keeps Services as real hash-indexed virtual servers (fast lookup
regardless of Service count) instead of a sequential chain of rules, and
because it's a borrowed general-purpose LB, it comes with its whole native
scheduler menu (`rr`, `wrr`, `lc`, `wlc`, `sh`, `dh`, `sed`, `nq`) as a side
effect — Kubernetes didn't write those, it just exposes a knob
(`--ipvs-scheduler`) to pick one.

**On this cluster, IPVS is not in use at all** — confirmed via `lsmod | grep vs`
(no `ip_vs` kernel module loaded) and no `ipvsadm` binary installed. It would
need to be deliberately enabled (kernel modules + `ipvsadm` package + a
`kube-proxy-arg: proxy-mode=ipvs` config change + a control-plane restart), and
at our scale (a handful of Services, one Node) the iptables approach above is
already fast enough that there's no real reason to.

---

## 7. Ingress and the Ingress Controller — an extra, optional layer *above* Services

A Service (§6) only understands IP addresses and ports — it has no idea what
HTTP is. If you want routing based on hostname (`chat.example.com` vs
`admin.example.com`) or URL path, or TLS termination, you need something that
actually speaks HTTP. That's an **Ingress controller**.

Crucially: an Ingress controller is **not** a special built-in Kubernetes
mechanism — it's just an ordinary Pod, running an ordinary reverse proxy
program, that happens to watch a special kind of object (`Ingress`) and
reconfigure itself accordingly. k3s ships one by default: **Traefik**.

```
$ kubectl get pods -n kube-system | grep traefik
traefik-59b7647586-vcb2t   1/1   Running
```

We created an `Ingress` pointing host `llm.local` at our Service:

```yaml
spec:
  ingressClassName: traefik
  rules:
    - host: llm.local
      http:
        paths:
          - path: /
            backend:
              service: { name: llm-vllm-pinned10, port: { number: 80 } }
```

### 7a. Ingress does *not* just forward to the Service — it does its own load balancing

This is the single most counter-intuitive finding of this whole exercise, and
we proved it empirically rather than assuming it. We fired requests through
Traefik and, *during* the requests, inspected Traefik's own live TCP
connections on the box:

```
request 1 → 10.42.0.73:8000   (Pod -a, direct!)
request 2 → 10.42.0.80:8000   (Pod -j, direct!)
request 3 → 10.42.0.74:8000   (Pod -d, direct!)
request 4 → 10.42.0.72:8000   (Pod -b, direct!)
...
```

**Not one of these went to the Service's ClusterIP (`10.43.194.231`) at all.**
Traefik watches the same `EndpointSlice` object kube-proxy watches (the live
list of Pod IPs behind the Service), but instead of sending traffic to the
Service and letting kube-proxy's iptables rules pick a Pod (§6a), it resolves
that Pod list *itself* and opens connections **directly to individual Pod
IPs**, spreading them with its own client-side load balancer (default:
round-robin — you can see it visiting different Pods each time above). Going
through the ClusterIP a second time would just be a redundant NAT hop with no
benefit, so real ingress controllers (Traefik, nginx-ingress, etc.) skip it.

So there are genuinely **two independent, separately-implemented load-balancing
mechanisms** present in this stack — kube-proxy's iptables coin-flip chain
(§6a) and Traefik's own round-robin — and only one of them is actually
exercised per request, depending which entry point you used.

### 7b. How external traffic reaches Traefik at all: k3s's ServiceLB (`klipper-lb`)

Traefik's own Service is `type: LoadBalancer` — a Service type that, on a real
cloud, causes the cloud provider to provision an actual external load balancer
and give you a public IP. There's no cloud provider here, so k3s fakes it with
its own tiny built-in mechanism, **ServiceLB** (formerly "Klipper LB"): a
`DaemonSet` (one Pod per Node — just one here) whose containers bind a real
port on the Node itself and forward into the Service:

```
$ kubectl -n kube-system get pod -l svccontroller.k3s.cattle.io/svcname=traefik -o yaml | ...
image: rancher/klipper-lb:v0.4.17
env:
  SRC_PORT=80, DEST_PORT=80, DEST_IPS=10.43.68.199   ← Traefik's own Service ClusterIP
```

So the *complete* path for an external request through the Ingress, all four
hops:

```
1. client → node IP 10.194.198.72:80        (klipper-lb / svclb-traefik, bound hostPort)
2.    → DNAT to Traefik's own Service ClusterIP:80    (klipper-lb's own simple iptables rule)
3.       → kube-proxy's normal Service DNAT picks a Traefik Pod   (only 1 replica here)
4.          → Traefik: matches Host header, then its OWN load balancer
               picks a vLLM Pod IP directly, bypassing kube-proxy again
```

---

## 8. Two full, concrete request paths through this exact system

Once the 10-instance vLLM deployment and the Ingress both exist simultaneously,
there are two genuinely different ways to reach it:

**Path A — direct to the Service (what most of our testing used):**
```
curl → Service ClusterIP 10.43.194.231:80
     → kube-proxy iptables rule (§6a), random pick of 1-of-10 Pods
     → Pod IP:8000 → vLLM process (taskset-pinned to its core range)
```
Only reachable from inside the cluster network, or from the Node itself.

**Path B — through the Ingress:**
```
curl -H "Host: llm.local" → node IP 10.194.198.72:80
     → klipper-lb → Traefik's Service → Traefik Pod
     → Traefik's own L7 routing + its own load balancer
     → Pod IP:8000 directly → vLLM process (taskset-pinned)
```
Reachable from any machine that can route to the Node's real IP, not just from
the Node itself — because it goes through the Node's real network interface,
not a cluster-internal virtual IP.

Both paths end up at the same 10 taskset-pinned Pods; they differ only in *how*
a specific Pod gets picked, and in whether the entry point is
cluster-internal-only or externally reachable.

---

## 9. Glossary

| Term | In one sentence, grounded in this system |
|---|---|
| **Container** | One packaged, isolated process — e.g., the vLLM `api_server` process. |
| **Pod** | The smallest thing Kubernetes schedules: one or more containers sharing one IP. Each of our 10 pinned instances is one Pod. |
| **Node** | A machine running Pods. We have exactly one: `volcano-a942-host`. |
| **Cluster** | Control plane + Node(s) together. Ours is k3s, single-Node. |
| **Namespace** | A naming partition, like a folder. Ours: `aim-demo-standalone`. |
| **Deployment** | "Keep N copies of this Pod template running." We use 10 separate Deployments (1 replica each) instead of 1×10, specifically to give each Pod its own `taskset` core range. |
| **Label / selector** | A key-value tag on a Pod, and a query that matches Pods by it. This is how a Service finds "its" Pods, and how our 10 separate Deployments still count as one logical group (shared `app` label). |
| **EndpointSlice** | The live, auto-maintained list of real Pod IPs currently matching a Service's selector and passing their readiness check. |
| **Service (ClusterIP)** | A stable virtual IP that always represents "whichever Pods currently match this selector." Ours: `llm-vllm-pinned10` → `10.43.194.231`. |
| **kube-proxy** | The component that makes a Service's virtual IP actually work, by programming the Node's kernel packet filter (iptables here) to DNAT it to real Pod IPs. On k3s it's code inside the `k3s server` process, not a separate Pod. |
| **iptables `statistic` match** | The actual primitive kube-proxy uses for "load balancing" in its default mode: a chain of independent, decreasing-probability coin-flip rules — not a named algorithm. |
| **IPVS** | An optional, *alternate* kube-proxy backend — a real in-kernel L4 load balancer borrowed from the older LVS project, which is where the "textbook" `rr`/`wrr`/`lc`/`sh` scheduler names actually come from. Not used on this cluster. |
| **Ingress** | An object describing HTTP-level routing rules (host/path → Service). Does nothing by itself — needs a controller to act on it. |
| **Ingress controller** | An ordinary Pod (here: Traefik, k3s's default) running a real HTTP reverse proxy, which watches `Ingress` objects and does the actual L7 routing — and, as we proved, its own direct-to-Pod load balancing, bypassing the Service/kube-proxy layer for the final hop. |
| **LoadBalancer (Service type)** | A Service type meant to be fulfilled by a cloud provider's real external LB. On bare-metal k3s, faked by k3s's own `ServiceLB`/`klipper-lb` DaemonSet, which just binds a real port on the Node and forwards into the Service. |
| **`taskset`** | A plain Linux tool (nothing Kubernetes-specific) that restricts which CPU cores a process may run on. The actual mechanism behind all the "core pinning" in this project. |
| **`os.execv()`** | The Python/POSIX call that replaces a process's program in place, keeping its PID and CPU affinity mask — why `taskset`'s restriction, set on the wrapper script, survives into the real vLLM/llama.cpp engine process after the handoff. |
