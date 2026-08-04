import { NextResponse } from "next/server";
import { gateway, GatewayError } from "@/lib/gateway";
import type { Invocation } from "@/lib/types";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const params = new URL(request.url).searchParams;
  const query = new URLSearchParams();
  query.set("limit", params.get("limit") ?? "50");
  const status = params.get("status");
  if (status) query.set("status_filter", status);
  const connector = params.get("connector_id");
  if (connector) query.set("connector_id", connector);

  try {
    return NextResponse.json(
      await gateway<Invocation[]>(`/v1/invocations?${query.toString()}`),
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
