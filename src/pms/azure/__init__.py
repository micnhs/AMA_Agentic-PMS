"""Azure implementations behind the service interfaces of :mod:`pms.interfaces` (APP_ENV=azure only).

* :mod:`pms.azure.cosmos_store` - case store on Azure Cosmos DB for NoSQL (containers ``cases``,
  ``signals``, ``records`` and the create-only, hash-chained ``audit`` container), the shared
  analysis-job queue and the registry of published Foundry agent versions.
* :mod:`pms.azure.blob_store` - letters, reports and outbox copies in Blob Storage (REST + Entra token).
* :mod:`pms.azure.search` - knowledge search on Azure AI Search with the offline search contract.
* :mod:`pms.azure.identity` - managed identity in Azure, the developer sign-in elsewhere.

Nothing here is imported in the offline mode: :mod:`pms.runtime` imports these modules only when
``APP_ENV=azure``, and every Azure SDK import happens inside functions.
"""
