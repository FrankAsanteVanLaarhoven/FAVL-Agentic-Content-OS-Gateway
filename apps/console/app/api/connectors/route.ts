import { NextResponse } from "next/server";
import { gateway, GatewayError } from "@/lib/gateway";
import type { Connector } from "@/lib/types";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const includeDeleted = new URL(request.url).searchParams.get("include_deleted");
  try {
    return NextResponse.json(
      await gateway<Connector[]>(
        `/v1/connectors${includeDeleted === "true" ? "?include_deleted=true" : ""}`,
      ),
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
