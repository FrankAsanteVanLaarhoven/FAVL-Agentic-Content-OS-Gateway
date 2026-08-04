# Security controls

1. OIDC bearer-token validation at APISIX.
2. Realm roles mapped to route-level authorization policies.
3. No connector credentials persisted in the registry.
4. External secrets injected at runtime.
5. Rate limits at gateway and workload levels.
6. Request IDs and distributed tracing.
7. Default-deny Kubernetes network policies.
8. TLS required in production.
9. Immutable image digests and signed SBOMs.
10. Audit events published to a dedicated durable NATS stream.

## Required hardening before production

- Replace development Keycloak.
- Disable direct access grants.
- Enforce PKCE for browser clients.
- Add APISIX authorization checks based on token claims.
- Store APISIX and Keycloak secrets in Vault, AWS Secrets Manager, or GCP Secret Manager.
- Add mTLS for service-to-service traffic.
- Add payload size limits and schema validation.
- Add WAF and automated-traffic controls at the edge.
- Add tenant-aware quotas and cost controls.
