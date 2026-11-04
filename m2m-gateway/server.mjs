import { randomUUID } from "node:crypto";
import express from "express";
import { CognitoJwtVerifier } from "aws-jwt-verify";
import {
  FetchError,
  JwtInvalidClaimError,
  JwtInvalidSignatureAlgorithmError,
  JwtInvalidSignatureError,
  JwtParseError,
  JwtWithoutValidKidError,
  KidNotFoundInJwksError,
  ParameterValidationError,
} from "aws-jwt-verify/error";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { z } from "zod";

function required(name) {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`Set ${name} before starting the server.`);
  return value;
}

const publicUrl = new URL(required("MCP_PUBLIC_URL"));
const localHttp = publicUrl.protocol === "http:"
  && ["localhost", "127.0.0.1"].includes(publicUrl.hostname);
if (publicUrl.protocol !== "https:" && !localHttp) {
  throw new Error("MCP_PUBLIC_URL must use HTTPS, except for local loopback.");
}
const clientIds = required("COGNITO_CLIENT_IDS").split(",").map((id) => id.trim());
if (clientIds.some((id) => !id)) throw new Error("COGNITO_CLIENT_IDS contains an empty ID.");
const verifier = CognitoJwtVerifier.create({
  userPoolId: required("COGNITO_USER_POOL_ID"),
  tokenUse: "access",
  clientId: clientIds,
});
const requiredScope = "mcp-demo/inventory.read";
const port = Number(process.env.PORT ?? "8080");
if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("Invalid PORT.");

const app = express();
app.disable("x-powered-by");
app.get("/healthz", (_req, res) => res.json({ status: "ok" }));

app.use("/mcp", async (req, res, next) => {
  const requestId = randomUUID();
  res.set("x-request-id", requestId);
  const origin = req.get("origin");
  if (origin && origin !== publicUrl.origin) {
    return res.status(403).json({ error: "Origin not allowed", requestId });
  }
  const match = /^Bearer (\S+)$/i.exec(req.get("authorization") ?? "");
  if (!match) {
    res.set("WWW-Authenticate", "Bearer");
    return res.status(401).json({ error: "Bearer token required", requestId });
  }
  let payload;
  try {
    payload = await verifier.verify(match[1]);
  } catch (error) {
    // With fixed startup configuration, issuer-selection errors indicate a bad token.
    const invalidToken = [
      JwtInvalidClaimError, JwtInvalidSignatureAlgorithmError,
      JwtInvalidSignatureError, JwtParseError, JwtWithoutValidKidError,
      KidNotFoundInJwksError, ParameterValidationError,
    ].some((type) => error instanceof type);
    console.warn(JSON.stringify({
      event: invalidToken ? "token_rejected" : "verification_unavailable",
      requestId,
      errorType: error.constructor.name,
    }));
    if (invalidToken) {
      res.set("WWW-Authenticate", 'Bearer error="invalid_token"');
      return res.status(401).json({ error: "Invalid access token", requestId });
    }
    if (error instanceof FetchError) res.set("Retry-After", "5");
    return res.status(503).json({ error: "Token verification unavailable", requestId });
  }
  if (!Number.isInteger(payload.exp) || payload.exp <= Math.floor(Date.now() / 1000)) {
    return res.status(401).json({ error: "Missing or expired token lifetime", requestId });
  }
  if (typeof payload.scope !== "string" || !payload.scope.split(" ").includes(requiredScope)) {
    res.set("WWW-Authenticate", `Bearer error="insufficient_scope", scope="${requiredScope}"`);
    return res.status(403).json({ error: "Required scope missing", requestId });
  }
  console.info(JSON.stringify({
    event: "authorized",
    requestId,
    clientId: payload.client_id,
    gatewayCorrelationId: req.get("x-correlation-id")?.slice(0, 64),
  }));
  next();
});

function inventoryServer() {
  const server = new McpServer({ name: "aws-inventory-demo", version: "1.0.0" });
  server.registerTool("get_inventory", {
    description: "Read fictional inventory for a demo SKU. No production data.",
    inputSchema: { sku: z.string().min(1).max(64) },
    annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
  }, async ({ sku }) => {
    if (sku !== "WIDGET-001") {
      return {
        isError: true,
        content: [{ type: "text", text: "Unknown demo SKU; use WIDGET-001." }],
      };
    }
    const result = {
      sku,
      quantity: 42,
      warehouse: "ATL-DEMO",
      source: "fictional-demo-data",
    };
    return {
      content: [{ type: "text", text: JSON.stringify(result) }],
      structuredContent: result,
    };
  });
  return server;
}

app.post("/mcp", express.json({ limit: "32kb" }), async (req, res) => {
  const server = inventoryServer();
  const transport = new StreamableHTTPServerTransport({
    sessionIdGenerator: undefined,
    enableJsonResponse: true,
  });
  res.once("close", () => {
    server.close().catch((error) => console.error("MCP cleanup failed:", error.name));
  });
  await server.connect(transport);
  await transport.handleRequest(req, res, req.body);
});

app.all("/mcp", (_req, res) => {
  res.set("Allow", "POST");
  res.status(405).json({ error: "This stateless MCP endpoint supports POST only." });
});

app.use((error, _req, res, _next) => {
  console.error("MCP request failed:", error.name);
  if (res.headersSent) {
    res.destroy();
    return;
  }
  const status = error.type === "entity.parse.failed" ? 400
    : error.type === "entity.too.large" ? 413 : 500;
  res.status(status).json({ error: status === 500 ? "MCP request failed" : "Invalid request body" });
});

const httpServer = app.listen(port, "0.0.0.0", () => {
  console.info(`Inventory MCP listening on port ${port}; public origin ${publicUrl.origin}`);
});
httpServer.on("error", (error) => {
  console.error("HTTP server failed:", error.message);
  process.exitCode = 1;
});
process.on("SIGTERM", () => httpServer.close());
process.on("SIGINT", () => httpServer.close());
