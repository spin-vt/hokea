# The hokea runner image: what a `hokea test` Job runs in the CLIENT
# namespace. It carries hokea + pytest + kubectl; inside the cluster,
# kubectl authenticates via the pod's mounted ServiceAccount
# (hokea-runner), so pytest's KubeCluster can create server pods in the
# paired team namespace with no kubeconfig at all.
#
# Build (context = repo root, so the image gets src/hokea + pyproject):
#     docker build -f runner/Dockerfile -t hokea-runner:<VERSION> .
# The tag is pinned in src/hokea/runner.py (RUNNER_IMAGE). Every release
# is a NEW tag, never a mutated one: the image is side-loaded onto the
# cluster nodes and pulled with IfNotPresent, so a reused tag would
# silently keep serving whatever each node had cached.

FROM python:3.12-slim

# kubectl, pinned and verified. v1.35.8 = the newest 1.35 client, matching
# the class cluster's v1.35.7 server. The sha512 is dl.k8s.io's published
# checksum for this exact binary; a mismatched download fails the build.
ARG KUBECTL_VERSION=v1.35.8
ARG KUBECTL_SHA512=56ee1b65e8289d9f20b103bb1dd9ac9067eb14aa7546e31884653ee82f6faee53644baa4381b783f98f32898849ccdf4054383b11e925bb8857a92e1714354ed
RUN python -c "import urllib.request; urllib.request.urlretrieve( \
        'https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl', \
        '/usr/local/bin/kubectl')" \
    && echo "${KUBECTL_SHA512}  /usr/local/bin/kubectl" | sha512sum -c - \
    && chmod 0755 /usr/local/bin/kubectl

# hokea itself, from the source in this repo, plus the test deps a student
# project needs. requests/pyyaml are hokea dependencies anyway; naming them
# keeps the runner's contract explicit.
COPY pyproject.toml /opt/hokea/pyproject.toml
COPY src /opt/hokea/src
RUN pip install --no-cache-dir /opt/hokea pytest requests pyyaml

# /work is where the launcher mounts the project ConfigMap (read-only);
# /project is the writable copy the entrypoint runs pytest in.
RUN mkdir -p /work /project
ENV PYTHONUNBUFFERED=1
COPY runner/entrypoint.sh /usr/local/bin/hokea-runner-entrypoint
RUN chmod 0755 /usr/local/bin/hokea-runner-entrypoint

ENTRYPOINT ["/usr/local/bin/hokea-runner-entrypoint"]
