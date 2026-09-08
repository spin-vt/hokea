# Running your system

Both backends present the same surface: a cluster object you enter with
`with ... as c:`, numbered nodes (1-based, matching `NODE_ID`), the same
process faults, and the same [PEERS contract](../lab.md#12-copy-the-toy-project-three-files). Tests written
against one backend run unchanged on the other — the only thing that changes
is which class your `hokea_cluster` fixture constructs.

Start locally with `Cluster` (Docker); move to `KubeCluster` when you deploy
to the class Kubernetes cluster.

## Docker backend

::: hokea.cluster
    options:
      members: false
      show_root_full_path: true
      heading_level: 3

For example:

```python
with Cluster(image="python:3.12-slim", src="./myserver",
             cmd="python server.py", nodes=5) as c:
    c.wait_healthy()
    ...
```

::: hokea.cluster.Cluster
    options:
      heading_level: 3
      members:
        - up
        - down
        - wait_healthy
        - node
        - nodes
        - make_net
        - make_chaos
        - kill
        - stop
        - start
        - pause
        - unpause
        - throttle
        - unthrottle
        - on_state_change

::: hokea.cluster.Node
    options:
      heading_level: 3
      members:
        - url
        - host_port
        - cluster_ip
        - exec
        - logs
        - is_running
        - status

::: hokea.cluster.dataset_mounts
    options:
      heading_level: 3

## Kubernetes backend

::: hokea.kube
    options:
      members: false
      show_root_full_path: true
      heading_level: 3

For example:

```python
with KubeCluster(image="python:3.12-slim", src="./myserver",
                 cmd="python server.py", nodes=5,
                 namespace="team-NN") as c:
    c.wait_healthy()
    ...
```

::: hokea.kube.KubeCluster
    options:
      heading_level: 3
      members:
        - up
        - down
        - wait_healthy
        - node
        - nodes
        - make_net
        - make_chaos
        - kill
        - stop
        - start
        - pause
        - throttle
        - unthrottle
        - on_state_change

::: hokea.kube.KubeNode
    options:
      heading_level: 3
      members:
        - url
        - host_port
        - cluster_ip
        - exec
        - logs
        - is_running
        - status
