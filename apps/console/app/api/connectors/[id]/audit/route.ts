import { NextResponse } from "next/server";
import { gateway, GatewayError } from "@/lib/gateway";
import type { AuditEntry } from "@/lib/types";

export const dynamic = "force-dynamic";

// Read-only. Transitions are deliberately not proxied: revoking a connector is
// a privileged, one-way, reason-bearing act, and the console has no privilege
// model yet to decide who may perform one. A button that could not be
// justified to an auditor does not belong here until it can be.
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  // The id goes into a path segment. Anything that is not a UUID is refused
  // here rather than forwarded, so a crafted value cannot reach for another
  // path on the gateway.
  if (!/^[0-9a-f-]{36}$/i.test(id)) {
    return NextResponse.json({ error: "invalid connector id" }, { status: 400 });
  }
  try {
    return NextResponse.json(
      await gateway<AuditEntry[]>(`/v1/connectors/${id}/audit`),
    );
  } catch (error) {
    if (error instanceof GatewayError) {
      return NextResponse.json(
        { error: error.message, detail: error.detail },
        { status: error.status },
      );
    }
    return NextResponse.json({ error: String(error) }, { status: 502 });
  }
}
