// Mirrors ConnectorState in services/connector-registry/app/lifecycle.py.
// The registry gained five states in migration 0008 while this union still
// listed five; the API was returning `validated` and `revoked` to a type that
// said they could not occur, and connectorTone's switch fell through to
// undefined for them.
export type ConnectorStatus =
  | "draft"
  | "installed"
  | "configured"
  | "validated"
  | "enabled"
  | "disabled"
  | "revoked"
  | "deletion_requested"
  | "archived"
  | "deleted";

export type Connector = {
  id: string;
  name: string;
  kind: "http" | "webhook" | "internal" | "mcp";
  base_url: string | null;
  scopes: string[];
  config: Record<string, unknown>;
  status: ConnectorStatus;
  version: number;
  created_at: string;
  deletion_requested_at: string | null;
  deleted_at: string | null;
  revoked_at?: string | null;
  archived_at?: string | null;
  credentials_rotated_at?: string | null;
  state_reason?: string | null;
  supports_idempotency?: boolean;
  idempotency_mode?: string;
};

export type Agent = {
  id: string;
  name: string;
  description: string;
  connector_ids: string[];
  created_at: string;
};

export type InvocationStatus =
  | "accepted"
  | "running"
  | "succeeded"
  | "failed_retryable"
  | "failed_terminal"
  | "timed_out"
  | "cancelled";

export type Invocation = {
  id: string;
  connector_id: string;
  connector_version: number;
  adapter_kind: string;
  idempotency_key: string;
  actor_id: string;
  tenant_id: string;
  operation: string;
  status: InvocationStatus;
  attempt: number;
  deadline_at: string;
  started_at: string | null;
  completed_at: string | null;
  provider_request_id: string | null;
  error_code: string | null;
  error_detail: string | null;
  retryable: boolean | null;
  trace_id: string | null;
  output: Record<string, unknown> | null;
  duration_ms: number | null;
};

export type OutboxStats = {
  pending: number;
  dead: number;
  published: number;
  oldest_pending_age_seconds: number | null;
  publisher_running: boolean;
};

export type Readiness = {
  status: string;
  database_connected: boolean;
  nats_connected: boolean;
  migrations_current?: boolean;
  required_stream_available?: boolean;
  adapters?: string[];
  outbox: OutboxStats | Record<string, never>;
};

export type Health = {
  gateway: "ok" | "degraded" | "down";
  orchestrator: Readiness | null;
  registry: Readiness | null;
};

/** State tone drives every accent in the UI. Nothing is coloured for brand. */
export type Tone = "ok" | "warn" | "err" | "info" | "idle";

export function invocationTone(status: InvocationStatus): Tone {
  switch (status) {
    case "succeeded":
      return "ok";
    case "accepted":
    case "running":
      return "info";
    case "failed_retryable":
    case "timed_out":
      return "warn";
    case "failed_terminal":
    case "cancelled":
      return "err";
  }
}

export function connectorTone(status: ConnectorStatus): Tone {
  switch (status) {
    case "enabled":
      return "ok";
    case "validated":
      return "ok";
    case "draft":
    case "installed":
    case "configured":
    case "disabled":
      return "idle";
    case "deletion_requested":
      return "warn";
    case "revoked":
    case "archived":
    case "deleted":
      return "err";
  }
}

/** One immutable lifecycle transition. Mirrors AuditEntry in the registry. */
export type AuditEntry = {
  id: string;
  connector_id: string;
  from_state: string;
  to_state: string;
  event: string;
  actor_id: string;
  reason: string | null;
  aggregate_version: number;
  recorded_at: string;
};

/** Tone for a transition, by the state it lands in. */
export function transitionTone(toState: string): Tone {
  return connectorTone(toState as ConnectorStatus) ?? "info";
}
