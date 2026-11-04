#Requires -Version 7.0
[CmdletBinding()]
param(
    [switch]$NoBrowser,
    # Azure OpenAI resource endpoint for the chat model; defaults to the sibling foundry-project azd environment.
    [string]$ModelEndpoint,
    [ValidatePattern('^[A-Za-z0-9._-]{1,64}$')][string]$ModelDeployment = "gpt-5-mini"
)

$ErrorActionPreference = "Stop"
$statePath = Join-Path $PSScriptRoot ".entra\config.json"
$secretPath = Join-Path $PSScriptRoot ".entra\client-secret.xml"
if (-not (Test-Path -LiteralPath $statePath) -or -not (Test-Path -LiteralPath $secretPath)) {
    throw "Run .\setup_entra.ps1 -SubscriptionId <subscription-id> first."
}
$state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
if ([datetimeoffset]$state.secretExpiresAt -le [datetimeoffset]::UtcNow) {
    throw "The web credential has expired. Run setup_entra.ps1 -SubscriptionId $($state.subscriptionId) -RotateSecret."
}

if (-not $ModelEndpoint) {
    $azdRoot = Join-Path $PSScriptRoot "..\foundry-project\.azure"
    $azdConfig = Join-Path $azdRoot "config.json"
    if (Test-Path -LiteralPath $azdConfig) {
        $environmentName = (Get-Content -LiteralPath $azdConfig -Raw | ConvertFrom-Json).defaultEnvironment
        $envFile = Join-Path $azdRoot "$environmentName\.env"
        if ($environmentName -match '^[A-Za-z0-9._-]+$' -and (Test-Path -LiteralPath $envFile)) {
            $line = Select-String -LiteralPath $envFile -Pattern '^AZURE_OPENAI_ENDPOINT=' | Select-Object -First 1
            if ($line) { $ModelEndpoint = $line.Line.Substring("AZURE_OPENAI_ENDPOINT=".Length).Trim().Trim('"') }
        }
    }
}
if ($ModelEndpoint) {
    # The model is called with this app's identity (your az login), never with the signed-in user's token.
    $modelTenant = if ($env:AZURE_OPENAI_TENANT_ID) { $env:AZURE_OPENAI_TENANT_ID } else { $state.tenantId }
    try {
        $null = & az account get-access-token --tenant $modelTenant --scope "https://cognitiveservices.azure.com/.default" `
            --query expiresOn --output tsv 2>$null
        $cliReady = $LASTEXITCODE -eq 0
    }
    catch { $cliReady = $false }
    if (-not $cliReady) {
        Write-Warning "Azure CLI has no token for tenant $modelTenant. Run 'az login --tenant $modelTenant' or chat answers will fail."
    }
    Write-Host "Chat model: $ModelDeployment at $ModelEndpoint (called as your az login identity)"
}
else {
    Write-Warning "No Azure OpenAI endpoint found; an offline responder answers instead. Pass -ModelEndpoint to use a model."
}

$credential = Import-Clixml -LiteralPath $secretPath
if ($credential.UserName -ne $state.webAppId) { throw "Protected credential does not match the configured web app." }
$settings = @{
    AUTH_MODE = "entra"; ENTRA_TENANT_ID = $state.tenantId
    OIDC_ISSUER = "https://login.microsoftonline.com/$($state.tenantId)/v2.0"
    OIDC_DISCOVERY_URL = "https://login.microsoftonline.com/$($state.tenantId)/v2.0/.well-known/openid-configuration"
    OIDC_CLIENT_ID = $state.webAppId; OIDC_CLIENT_SECRET = $credential.GetNetworkCredential().Password
    API_AUDIENCE = $state.apiAppId; API_SCOPE = "api://$($state.apiAppId)/inventory.read"
    WEB_URL = $state.webUrl; WEB_PORT = "$($state.webPort)"
    MCP_PORT = "$($state.mcpPort)"; MCP_PUBLIC_URL = "http://127.0.0.1:$($state.mcpPort)"
    MCP_URL = "http://127.0.0.1:$($state.mcpPort)/mcp"
    REQUIRED_SCOPE = "inventory.read"; REQUIRED_ROLE = "Inventory.Reader"
    AZURE_OPENAI_ENDPOINT = "$ModelEndpoint"; AZURE_OPENAI_DEPLOYMENT = $ModelDeployment
}
$previous = @{}
try {
    foreach ($name in $settings.Keys) {
        $previous[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
        [Environment]::SetEnvironmentVariable($name, $settings[$name], "Process")
    }
    $arguments = @( (Join-Path $PSScriptRoot "run_demo.py") )
    if ($NoBrowser) { $arguments += "--no-browser" }
    & python @arguments
    if ($LASTEXITCODE -ne 0) { throw "The Entra demo exited with code $LASTEXITCODE." }
}
finally {
    foreach ($name in $previous.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previous[$name], "Process")
    }
    $settings.OIDC_CLIENT_SECRET = $null
    $credential = $null
}
