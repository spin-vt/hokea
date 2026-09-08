# Faults: partitions, netem, chaos

Network faults come from the object the `net` fixture hands you — a `Net` on
the Docker backend, a `KubeNet` on Kubernetes, with the same methods and the
same rule: every fault is **verified** before your test continues, and
every action lands in the run's fault timeline (see the
[artifact formats](../artifacts.md)). Slowness faults use netem, Linux's
network-emulation tool. Process faults (`kill`, `stop`,
`throttle`, ...) live on the [cluster object](cluster.md) itself. On
Kubernetes only, `KubeChaos` (the `chaos` fixture) applies Chaos Mesh
experiment files (YAML) of your own design inside the fault schedule.

## Docker backend

::: hokea.net
    options:
      members: false
      show_root_full_path: true
      heading_level: 3

::: hokea.net.Net
    options:
      heading_level: 3
      members:
        - partition
        - isolate
        - slow
        - heal

## Kubernetes backend

::: hokea.kube.KubeNet
    options:
      heading_level: 3
      members:
        - partition
        - isolate
        - slow
        - heal

::: hokea.kube.KubeChaos
    options:
      heading_level: 3
      members:
        - apply
        - clear
