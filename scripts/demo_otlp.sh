#!/usr/bin/env bash

set -euo pipefail

kube_context="${KUBE_CONTEXT:-brownrook-k3s1}"
kube_namespace="${KUBE_NAMESPACE:-home-budget}"
client_secret="${OTLP_DEMO_CLIENT_SECRET:-receipt-telemetry-client-tls}"
collector_endpoint="${OTLP_DEMO_ENDPOINT:-otel-collector.home-budget.svc.cluster.local:4317}"
demo_service="${OTLP_DEMO_SERVICE:-home-budget-otlp-demo-$(date +%s)}"
pod_name="otlp-demo-$(date +%s)"
telemetrygen_image="${TELEMETRYGEN_IMAGE:-ghcr.io/open-telemetry/opentelemetry-collector-contrib/telemetrygen@sha256:9b2bdd7fe8bd3d3ac0f6705e53390a4868a38673e09cc1bc394ef2525ef235d1}"

for command_name in kubectl jq; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf 'Required command is not installed: %s\n' "$command_name" >&2
    exit 2
  fi
done

cleanup() {
  kubectl --context "$kube_context" -n "$kube_namespace" \
    delete pod "$pod_name" --ignore-not-found --wait=false >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

kubectl --context "$kube_context" -n "$kube_namespace" \
  get secret "$client_secret" >/dev/null
kubectl --context "$kube_context" -n "$kube_namespace" \
  rollout status deployment/otel-collector --timeout=60s >/dev/null

# The fsGroup makes the projected 0440 client key readable by the non-root
# telemetrygen process. No credential value is placed in the Pod command line.
overrides=$(jq -cn \
  --arg image "$telemetrygen_image" \
  --arg endpoint "$collector_endpoint" \
  --arg secret "$client_secret" \
  --arg service "$demo_service" \
  '{
    apiVersion: "v1",
    spec: {
      automountServiceAccountToken: false,
      securityContext: {
        runAsNonRoot: true,
        runAsUser: 10001,
        runAsGroup: 10001,
        fsGroup: 10001,
        fsGroupChangePolicy: "OnRootMismatch",
        seccompProfile: {type: "RuntimeDefault"}
      },
      containers: [{
        name: "otlp-demo",
        image: $image,
        args: [
          "traces", "--traces", "1",
          "--otlp-endpoint", $endpoint,
          "--ca-cert", "/tls/ca.crt",
          "--mtls",
          "--client-cert", "/tls/tls.crt",
          "--client-key", "/tls/tls.key",
          "--service", $service
        ],
        securityContext: {
          allowPrivilegeEscalation: false,
          capabilities: {drop: ["ALL"]},
          readOnlyRootFilesystem: true
        },
        volumeMounts: [{name: "tls", mountPath: "/tls", readOnly: true}]
      }],
      volumes: [{
        name: "tls",
        secret: {secretName: $secret, defaultMode: 288}
      }]
    }
  }')

printf 'Sending one synthetic trace as service %s...\n' "$demo_service"
kubectl --context "$kube_context" -n "$kube_namespace" run "$pod_name" \
  --restart=Never \
  --rm \
  -i \
  --pod-running-timeout=3m \
  --image="$telemetrygen_image" \
  --overrides="$overrides"

printf '\nWait at least 10 seconds for tail sampling, then query Tempo in Grafana:\n'
printf '{ resource.service.name = "%s" }\n' "$demo_service"
