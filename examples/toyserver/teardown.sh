#!/bin/sh
# Remove every trace hokea left in a namespace. hokea labels everything it
# creates (pods, services, configmaps, chaos CRs) and normally cleans up
# after itself; this sweeps a namespace after a crashed or interrupted run.
# Also catches hand-applied chaos objects (e.g. a StressChaos) that carry
# hokea's labels.
#
#   ./teardown.sh <namespace>            # remove hokea-created resources
#   ./teardown.sh <namespace> --nuke     # ...then delete the namespace too
set -eu
NS="${1:?usage: teardown.sh <namespace> [--nuke]}"

kubectl -n "$NS" delete pods,services,configmaps \
    -l app.kubernetes.io/managed-by=hokea --ignore-not-found --wait=true

# Chaos kinds go in their own kubectl: on a cluster without Chaos Mesh the
# CRDs don't exist, and that resource-mapping error must not abort the
# pod/service/configmap sweep above (hence the || true under set -e).
kubectl -n "$NS" delete \
    networkchaos,stresschaos,podchaos,iochaos,timechaos,httpchaos,dnschaos,kernelchaos,blockchaos,workflows.chaos-mesh.org \
    -l app.kubernetes.io/managed-by=hokea --ignore-not-found --wait=true \
    || true

if [ "${2:-}" = "--nuke" ]; then
    kubectl delete namespace "$NS" --wait=true
fi
