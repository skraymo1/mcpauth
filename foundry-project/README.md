# Foundry project (azd)

The Microsoft Foundry project, model deployment, and supporting resources used
by the M2M walkthrough's Foundry agent. The Entra chat demo can also use its
model deployment.

`azure.yaml` and `infra/` are an unmodified copy of Microsoft's MIT-licensed
[azd-ai-starter-basic](https://github.com/Azure-Samples/azd-ai-starter-basic)
template (see [THIRD-PARTY-NOTICES.md](../THIRD-PARTY-NOTICES.md)).

`evals/` belongs to this demo. `azure.eval.yaml` declares the turn-level
dataset `datasets/aws-inventory-agent-turn-tests.jsonl` (15 prompts for the
walkthrough's `aws-inventory-agent`, which also fill the chat demo's question
picker) and `evaluators/aws-inventory-agent-evaluator.json`, a weighted rubric
that checks the tool call, verbatim results, and no invented data.

## Provision

Requires the [Azure Developer CLI](https://aka.ms/install-azd) with the
`azure.ai.agents` extension, which `azure.yaml` requires. Section 7.1 of the
walkthrough also uses the `azure.ai.connections` extension.

```powershell
azd extension install azure.ai.agents
azd extension install azure.ai.connections
azd auth login
cd foundry-project
azd env new m2mmcp --location swedencentral --subscription <subscription-id>
azd env set AI_PROJECT_DEPLOYMENTS '[{"name":"gpt-5-mini","model":{"name":"gpt-5-mini","format":"OpenAI","version":"2025-08-07"},"sku":{"name":"GlobalStandard","capacity":50}}]'
azd provision
```

The template deploys no model unless `AI_PROJECT_DEPLOYMENTS` is set. Choose a
region where the model and SKU are available. The environment name `m2mmcp`
creates resource group `rg-m2mmcp`, which matches the walkthrough's commands.

azd writes the outputs, including `AZURE_AI_PROJECT_ENDPOINT` and
`AZURE_OPENAI_ENDPOINT`, to `.azure/<environment>/.env`. That folder is ignored
by Git; do not commit it.

Continue with [walkthrough section 7.1](../docs/m2m-walkthrough.md#71-scripted-foundry-setup-as-validated)
to connect the agent to APIM.

To clean up, `azd down --purge` deletes the whole `rg-<environment>` resource
group, including any walkthrough resources deployed into it, such as APIM, Key
Vault, and Redis. [Walkthrough section 10](../docs/m2m-walkthrough.md#10-failure-cases-and-production-boundaries)
covers the remaining cleanup, including the gateway API app registration.
