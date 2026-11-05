#Requires -Version 7.0
# Create the Foundry prompt agent that calls the MCP server through the APIM gateway. postprovision runs this as the
# signed-in user, who has the Azure AI User role on the project (ai-project.bicep assigns it). The agent is a data-plane
# object that ARM cannot create, so it is not part of main.bicep. An existing agent is left alone, which keeps edits made
# in the portal. Set CREATE_AGENT=false to skip this step.
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

if ("$env:CREATE_AGENT" -eq "false") {
    Write-Host "CREATE_AGENT is false; not creating the Foundry agent (see docs/m2m-walkthrough.md section 7.1)."
    return
}

foreach ($name in "AZURE_SUBSCRIPTION_ID", "FOUNDRY_PROJECT_ENDPOINT", "FOUNDRY_AGENT_NAME", "AZURE_OPENAI_DEPLOYMENT",
        "APIM_FOUNDRY_MCP_URL", "FOUNDRY_MCP_CONNECTION_NAME") {
    if (-not [Environment]::GetEnvironmentVariable($name)) {
        throw "$name is not set. Provisioning did not finish."
    }
}

$skipHint = "To skip this step, run 'azd env set CREATE_AGENT false' and create the agent by hand " +
    "(docs/m2m-walkthrough.md section 7.1)."
$endpoint = $env:FOUNDRY_PROJECT_ENDPOINT.TrimEnd("/")
$agentName = $env:FOUNDRY_AGENT_NAME
$approval = if ($env:FOUNDRY_AGENT_REQUIRE_APPROVAL) { $env:FOUNDRY_AGENT_REQUIRE_APPROVAL } else { "always" }
if ($approval -notin "always", "never") {
    throw "FOUNDRY_AGENT_REQUIRE_APPROVAL must be 'always' or 'never', not '$approval'."
}

$token = az account get-access-token --resource https://ai.azure.com --subscription $env:AZURE_SUBSCRIPTION_ID `
    --query accessToken --output tsv --only-show-errors
if ($LASTEXITCODE -ne 0 -or -not $token) {
    throw "Could not get a Foundry token from az. Run az login (azd auth login is separate), then rerun. $skipHint"
}

function Get-Excerpt([string]$Text) {
    if ($Text.Length -gt 500) { $Text.Substring(0, 500) } else { $Text }
}

# The Azure AI User role on the project can take a few minutes to take effect, so 401 and 403 are retried along
# with network errors, 429 and 5xx.
function Invoke-Foundry([string]$Method, [string]$Path, $Body = $null) {
    $request = @{
        Method = $Method
        Uri = "$endpoint$Path"
        Headers = @{ Authorization = "Bearer $token" }
        SkipHttpErrorCheck = $true
        TimeoutSec = 90
    }
    if ($null -ne $Body) {
        $request.Body = $Body | ConvertTo-Json -Depth 20 -Compress
        $request.ContentType = "application/json"
    }
    $attempts = 12
    for ($attempt = 1; ; $attempt++) {
        try {
            $response = Invoke-WebRequest @request
            $result = [pscustomobject]@{ Status = [int]$response.StatusCode; Text = [string]$response.Content }
        }
        catch {
            $result = [pscustomobject]@{ Status = 0; Text = $_.Exception.Message }
        }
        $retryable = ($result.Status -in 0, 401, 403, 429) -or ($result.Status -ge 500)
        if (-not $retryable -or $attempt -ge $attempts) { return $result }
        $reason = if ($result.Status -eq 0) { "a network error" } else { "HTTP $($result.Status)" }
        Write-Host "$Method $Path returned $reason; retrying in 30 s (attempt $attempt of $attempts)."
        Start-Sleep -Seconds 30
    }
}

$existing = Invoke-Foundry GET "/agents/$([uri]::EscapeDataString($agentName))?api-version=v1"
if ($existing.Status -eq 200) {
    Write-Host "Foundry agent $agentName already exists; leaving it as it is."
    return
}
if ($existing.Status -ne 404) {
    throw "Checking for agent $agentName failed with HTTP $($existing.Status): $(Get-Excerpt $existing.Text). $skipHint"
}

$instructions = @(
    "You answer inventory questions for a machine-to-machine (M2M) authentication demo.",
    ("Use the aws_inventory MCP tool get_inventory to look up a SKU. Report the quantity and warehouse exactly " +
        "as the tool returns them, and mention the cognitoClientId the AWS side saw when it is present."),
    ("If the tool fails or returns an error, say so plainly. Never invent inventory data. " +
        "All data is fictional demo data.")
) -join "`n"

$created = Invoke-Foundry POST "/agents?api-version=v1" @{
    name = $agentName
    description = "M2M demo agent created by the postprovision hook."
    definition = @{
        kind = "prompt"
        model = $env:AZURE_OPENAI_DEPLOYMENT
        instructions = $instructions
        tools = @(@{
            type = "mcp"
            server_label = "aws_inventory"
            server_url = $env:APIM_FOUNDRY_MCP_URL
            allowed_tools = @("get_inventory")
            require_approval = $approval
            project_connection_id = $env:FOUNDRY_MCP_CONNECTION_NAME
        })
    }
}
if ($created.Status -lt 200 -or $created.Status -ge 300) {
    throw "Creating agent $agentName failed with HTTP $($created.Status): $(Get-Excerpt $created.Text). $skipHint"
}
Write-Host "Created Foundry agent $agentName."
