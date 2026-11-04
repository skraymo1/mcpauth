#Requires -Version 7.0
$ErrorActionPreference = "Stop"

foreach ($name in @("MCP_URL", "COGNITO_TOKEN_URL", "COGNITO_CLIENT_ID", "COGNITO_CLIENT_SECRET")) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
        throw "Set $name before running this script."
    }
}
$mcpUrl = [uri]$env:MCP_URL
$tokenUrl = [uri]$env:COGNITO_TOKEN_URL
$localHttp = $mcpUrl.Scheme -eq "http" -and $mcpUrl.Host -in @("localhost", "127.0.0.1")
if ($tokenUrl.Scheme -ne "https" -or ($mcpUrl.Scheme -ne "https" -and -not $localHttp)) {
    throw "Use HTTPS endpoints; only the local MCP loopback endpoint may use HTTP."
}

$basic = [Convert]::ToBase64String(
    [Text.Encoding]::UTF8.GetBytes("$($env:COGNITO_CLIENT_ID):$($env:COGNITO_CLIENT_SECRET)")
)
$tokenResponse = Invoke-RestMethod -Uri $tokenUrl -Method Post `
    -Headers @{ Authorization = "Basic $basic" } `
    -ContentType "application/x-www-form-urlencoded" `
    -Body @{ grant_type = "client_credentials"; scope = "mcp-demo/inventory.read" } `
    -MaximumRedirection 0 -TimeoutSec 30
if ([string]::IsNullOrWhiteSpace($tokenResponse.access_token) -or
    $tokenResponse.token_type -ne "Bearer" -or $tokenResponse.expires_in -le 0) {
    throw "Cognito did not return a usable bearer access token."
}
Write-Host "Cognito issued an app-only token; token value is not displayed."
$headers = @{
    Authorization = "Bearer $($tokenResponse.access_token)"
    Accept = "application/json, text/event-stream"
}

function Send-McpMessage([hashtable]$Message) {
    $result = Invoke-RestMethod -Uri $mcpUrl -Method Post -Headers $headers `
        -ContentType "application/json" -Body ($Message | ConvertTo-Json -Depth 10 -Compress) `
        -MaximumRedirection 0 -TimeoutSec 30 -AllowUnencryptedAuthentication:$localHttp
    if ($result.error) {
        throw "MCP error $($result.error.code): $($result.error.message)"
    }
    return $result
}

$initial = Send-McpMessage @{
    jsonrpc = "2.0"; id = 1; method = "initialize"
    params = @{
        protocolVersion = "2025-06-18"
        capabilities = @{}
        clientInfo = @{ name = "m2m-demo"; version = "1.0.0" }
    }
}
if (-not $initial.result.protocolVersion) { throw "MCP initialization failed." }
$headers["MCP-Protocol-Version"] = $initial.result.protocolVersion
$null = Send-McpMessage @{ jsonrpc = "2.0"; method = "notifications/initialized" }
$tools = Send-McpMessage @{ jsonrpc = "2.0"; id = 2; method = "tools/list" }
Write-Host "Available tools: $($tools.result.tools.name -join ', ')"
$result = Send-McpMessage @{
    jsonrpc = "2.0"; id = 3; method = "tools/call"
    params = @{ name = "get_inventory"; arguments = @{ sku = "WIDGET-001" } }
}
if ($result.result.isError) { throw "The inventory tool reported an error." }
$result.result | ConvertTo-Json -Depth 10
