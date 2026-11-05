#Requires -Version 7.0
# Finish the work that needs deployed resources: the Entra redirect URI, the Mcp.Invoke grant for the Foundry project
# identity, and the Foundry agent (create-agent.ps1).
. "$PSScriptRoot\..\..\entra-chat-demo\infra\hooks\common.ps1"

$context = Get-HookContext
foreach ($name in "WEB_URL", "FOUNDRY_PROJECT_PRINCIPAL_ID", "ENTRA_GATEWAY_API_CLIENT_ID") {
    if (-not [Environment]::GetEnvironmentVariable($name)) {
        throw "$name is not set. Provisioning did not finish, or the preprovision hook did not run."
    }
}

$webMatches = @(Find-DemoApps $context.WebName)
if ($webMatches.Count -ne 1 -or $webMatches[0].tags -notcontains "EntraMcpChatDemo") {
    throw "Could not safely find the demo-owned web app registration $($context.WebName)."
}
$web = $webMatches[0]
$webUrl = $env:WEB_URL.TrimEnd("/")
$redirectUris = @($web.web.redirectUris) + @("$webUrl/auth/callback", "$webUrl/") |
    Where-Object { $_ } | Select-Object -Unique
$null = Invoke-GraphWithRetry PATCH "/applications/$($web.id)" @{
    web = @{
        redirectUris = @($redirectUris)
        implicitGrantSettings = @{ enableAccessTokenIssuance = $false; enableIdTokenIssuance = $false }
    }
}

$gatewaySuffix = "azd-$($env:AZURE_ENV_NAME)-$($env:AZURE_SUBSCRIPTION_ID.Substring(0, 8).ToLowerInvariant())"
$gatewayName = "mcp-m2m-gateway-api-$gatewaySuffix"
$gatewayMatches = @(Find-DemoApps $gatewayName)
if ($gatewayMatches.Count -ne 1 -or $gatewayMatches[0].tags -notcontains "EntraMcpChatDemo") {
    throw "Could not safely find the demo-owned gateway app registration $gatewayName."
}
$gateway = $gatewayMatches[0]
$invokeRole = @($gateway.appRoles | Where-Object { $_.value -eq "Mcp.Invoke" -and $_.isEnabled })
if ($invokeRole.Count -ne 1) {
    throw "Gateway app registration $gatewayName must have exactly one enabled Mcp.Invoke role."
}
$gatewayPrincipal = Get-DemoPrincipal $gateway.appId
Grant-DemoRole $env:FOUNDRY_PROJECT_PRINCIPAL_ID $gatewayPrincipal.id $invokeRole[0].id

Write-Host "Registered the chat app redirect URI and granted the Foundry project identity Mcp.Invoke on the gateway."

& "$PSScriptRoot\create-agent.ps1"
