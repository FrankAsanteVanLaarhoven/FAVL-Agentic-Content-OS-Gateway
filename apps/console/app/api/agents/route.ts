import { NextResponse } from "next/server";
import { gateway, GatewayError } from "@/lib/gateway";
import type { Agent } from "@/lib/types";

export const dynamic = "force-dynamic";

/**
 * Read-only proxy.
 *
 * A POST handler previously lived here. Because these routes attach the
 * console's server-side gateway token to every request, an unauthenticated
 * write endpoint on the console origin is a confused deputy: any page on the
 * internet could submit a form to it and create resources with the console's
 * credentials. The console has no create flow yet, so the safest handler is
 * no handler. When one is added it must require a same-origin check plus a
 * CSRF token — see requireSameOrigin in lib/origin.ts.
 */
export async function GET() {
  try {
    return NextResponse.json(await gateway<Agent[]>("/v1/agents"));
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
