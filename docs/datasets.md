# Course dataset images

*This page is the catalog of ready-made dataset images and the recipe for
publishing your own. For what `/dataset` and `/data` mean inside your
nodes — and how to pass a dataset to your cluster with `data=` — start with
[Data & durability](data-and-durability.md).*

A **dataset image** is an ordinary container image — a frozen filesystem
snapshot, the kind Docker builds and ships — whose contents *are* the
data: a two-line Dockerfile (the recipe file images are built from:
`FROM scratch` + `COPY data/ /`), no code, no shell, nothing to run.
hokea *mounts* it — makes it appear — read-only at `/dataset` in every
node, so getting the corpus onto every machine is one line of
configuration instead of a download step in every team's setup. Because they are
ordinary images, they version, cache, and distribute like any other
image — each cluster machine keeps **one on-disk copy** no matter how
many teams mount the same dataset, so a 50 MB corpus used by 30 teams
costs 50 MB per machine, not 1.5 GB.

## The menu

| image           | contents                                   | size (data) | license       | course project              |
|-----------------|--------------------------------------------|-------------|---------------|-----------------------------|
| `fashion-mnist` | 4 idx gz files, 70k fashion images         | ~30 MB      | MIT           | ML training                 |
| `mnist`         | 4 idx gz files, 70k handwritten digits     | ~11 MB      | CC BY-SA 3.0  | ML training                 |
| `wikitext-2`    | `wiki.{train,valid,test}.tokens` plain text | ~13 MB      | CC BY-SA 3.0  | MapReduce rollups, search   |
| `weblogs-synth` | 20 JSONL shards of synthetic ad logs       | ~56 MB      | CC0-1.0       | MapReduce rollups, scheduler |

Each image records its upstream source, license, and a one-line
description in its image labels (`docker inspect` shows them). The image
references are public and need no login to pull:

- `ghcr.io/spin-vt/datasets/fashion-mnist:v1`
- `ghcr.io/spin-vt/datasets/mnist:v1`
- `ghcr.io/spin-vt/datasets/wikitext-2:v1`
- `ghcr.io/spin-vt/datasets/weblogs-synth:v1`

## How hokea consumes a dataset

Pass the source as `data=` and the contents appear read-only at `/dataset`
in every node. On the Kubernetes backend (`KubeCluster`) a source is an
image reference — the image's name-plus-tag:

```python
cluster = KubeCluster(..., data="ghcr.io/spin-vt/datasets/wikitext-2:v1")
# node code: open("/dataset/wiki.train.tokens")
```

The full mechanics — the Docker backend's live-directory form, mounting
several datasets at once with the dict form, the private writable `/data`
scratch space, and the sharding convention — are on the
[Data & durability](data-and-durability.md) page.

## Publishing your own dataset image

Any team can publish a dataset the same way, to any **registry** they
can push to — a registry is a server that hosts images for others to
download, like a package index for container images; ghcr.io, GitHub's
registry, is free for public images. As long as the image is public,
the class cluster downloads it directly, exactly like your laptop
would.

```sh
mkdir mydata && cd mydata
mkdir data                    # put your files in data/ (keep it < ~100 MB)
cat > Dockerfile <<'EOF'
FROM scratch
COPY data/ /
LABEL org.opencontainers.image.title="my-corpus" \
      org.opencontainers.image.licenses="CC0-1.0" \
      org.opencontainers.image.version="v1"
EOF
docker build -t ghcr.io/<you>/my-corpus:v1 .
docker push ghcr.io/<you>/my-corpus:v1
```

On ghcr.io, one last click makes it public: your GitHub profile →
*Packages* → `my-corpus` → *Package settings* → *Change visibility* →
**Public** (the introductory lab's Approach B walks through the same
account-setup and push steps for a server image).

Then `data="ghcr.io/<you>/my-corpus:v1"`, and the cluster pulls it on
first use. (If you have data you can't publish publicly, ask me; I can
load an unpublished image onto the cluster's machines by hand.) Three
rules: include a license label you actually have the rights to grant,
never bake in credentials or personal data, and cut a new tag (`v2`)
instead of changing `v1` in place. Nodes pull with `IfNotPresent`
(Kubernetes-speak for "download only if this tag isn't already on the
machine"), so a node that already holds a `v1` never re-fetches it, and a
changed `v1` won't propagate.

## Licenses

| dataset         | license      | obligations for course use                          |
|-----------------|--------------|-----------------------------------------------------|
| `fashion-mnist` | MIT          | keep the copyright notice (in the image labels/README) |
| `mnist`         | CC BY-SA 3.0 | attribution (LeCun et al.); share derived data alike |
| `wikitext-2`    | CC BY-SA 3.0 | attribution (Merity et al. / Wikipedia); share-alike |
| `weblogs-synth` | CC0-1.0      | none — synthetic, generated from a script            |
