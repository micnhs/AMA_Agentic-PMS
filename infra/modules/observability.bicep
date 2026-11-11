// Operations overview and alert rules (concept 6.6, ADR-009). Generally available features only;
// preview monitoring stays off (OI-032).
//
// Telemetry contract with the app (OpenTelemetry log records -> Application Insights 'traces',
// attributes -> customDimensions): pms_event = ALERT_SENT | AGENT_RUN | WORKER_HEARTBEAT |
// AUDIT_VERIFY | CASE_OVERDUE, plus pms_status (ok|failed), pms_latency_s, pms_case_id, pms_agent,
// pms_model, pms_duration_ms, pms_tokens_in, pms_tokens_out, pms_cost_usd, pms_records.
// The budget alerts are budget notifications (governance.bicep).
param location string
param tags object
param namePrefix string
param appInsightsId string
param deployerEmail string

@description('Evaluation frequency and window of the log alert rules (ISO 8601).')
param evaluationFrequency string = 'PT15M'

param alertRulesEnabled bool = true

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'ag-${namePrefix}'
  location: 'global'
  tags: tags
  properties: {
    groupShortName: 'pmsdemo'
    enabled: true
    emailReceivers: [
      {
        name: 'deployer'
        emailAddress: deployerEmail
        useCommonAlertSchema: true
      }
    ]
  }
}

var alertDefinitions = [
  {
    key: 'alert-latency'
    displayName: 'PMS demo: critical-case alert later than 60 seconds'
    description: 'A critical-case alert was sent more than 60 seconds after the letter was received (pilot target, concept 6.4).'
    severity: 1
    query: 'traces | where tostring(customDimensions.pms_event) == "ALERT_SENT" | where todouble(customDimensions.pms_latency_s) > 60'
    operator: 'GreaterThan'
    threshold: 0
  }
  {
    key: 'agent-failures'
    displayName: 'PMS demo: failed agent runs'
    description: 'At least one agent run failed. Triage and alerting continue independently; the case shows the failure.'
    severity: 2
    query: 'traces | where tostring(customDimensions.pms_event) == "AGENT_RUN" | where tostring(customDimensions.pms_status) == "failed"'
    operator: 'GreaterThan'
    threshold: 0
  }
  {
    key: 'worker-heartbeat-missing'
    displayName: 'PMS demo: alert worker heartbeat missing'
    description: 'The alert worker sent no heartbeat in the evaluation window; critical-case e-mails may be delayed.'
    severity: 1
    query: 'traces | where tostring(customDimensions.pms_event) == "WORKER_HEARTBEAT"'
    operator: 'LessThan'
    threshold: 1
  }
  {
    key: 'audit-chain-failure'
    displayName: 'PMS demo: audit chain verification failed'
    description: 'The hash chain of the audit trail did not verify; a record may have been changed outside the app.'
    severity: 1
    query: 'traces | where tostring(customDimensions.pms_event) == "AUDIT_VERIFY" | where tostring(customDimensions.pms_status) == "failed"'
    operator: 'GreaterThan'
    threshold: 0
  }
]

resource alertRules 'Microsoft.Insights/scheduledQueryRules@2021-08-01' = [for definition in alertDefinitions: {
  name: '${namePrefix}-${definition.key}'
  location: location
  tags: tags
  kind: 'LogAlert'
  properties: {
    displayName: definition.displayName
    description: definition.description
    severity: definition.severity
    enabled: alertRulesEnabled
    scopes: [
      appInsightsId
    ]
    evaluationFrequency: evaluationFrequency
    windowSize: evaluationFrequency
    criteria: {
      allOf: [
        {
          query: definition.query
          timeAggregation: 'Count'
          operator: definition.operator
          threshold: definition.threshold
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: {
      actionGroups: [
        actionGroup.id
      ]
    }
  }
}]

var day = 86400000
var workbookQueries = [
  {
    name: 'agent-runs'
    title: 'Agent runs, failures and duration by agent and model (24 h)'
    query: 'traces | where tostring(customDimensions.pms_event) == "AGENT_RUN" | summarize runs = count(), failed = countif(tostring(customDimensions.pms_status) == "failed"), p95_duration_ms = percentile(todouble(customDimensions.pms_duration_ms), 95) by agent = tostring(customDimensions.pms_agent), model = tostring(customDimensions.pms_model) | order by agent asc'
    visualization: 'table'
  }
  {
    name: 'tokens-cost'
    title: 'Tokens and estimated model cost by agent (24 h, price table of the app)'
    query: 'traces | where tostring(customDimensions.pms_event) == "AGENT_RUN" | summarize tokens_in = sum(tolong(customDimensions.pms_tokens_in)), tokens_out = sum(tolong(customDimensions.pms_tokens_out)), cost_usd = round(sum(todouble(customDimensions.pms_cost_usd)), 4) by agent = tostring(customDimensions.pms_agent), model = tostring(customDimensions.pms_model)'
    visualization: 'table'
  }
  {
    name: 'time-to-alert'
    title: 'Time to critical-case alert in seconds (target below 60 s, pilot)'
    query: 'traces | where tostring(customDimensions.pms_event) == "ALERT_SENT" | project timestamp, case_id = tostring(customDimensions.pms_case_id), latency_s = todouble(customDimensions.pms_latency_s) | order by timestamp desc'
    visualization: 'table'
  }
  {
    name: 'overdue-cases'
    title: 'Overdue cases reported by the app (latest state per case)'
    query: 'traces | where tostring(customDimensions.pms_event) == "CASE_OVERDUE" | summarize last_seen = max(timestamp) by case_id = tostring(customDimensions.pms_case_id) | order by last_seen desc'
    visualization: 'table'
  }
  {
    name: 'audit-chain'
    title: 'Audit chain verification (latest 20)'
    query: 'traces | where tostring(customDimensions.pms_event) == "AUDIT_VERIFY" | project timestamp, status = tostring(customDimensions.pms_status), records = tolong(customDimensions.pms_records) | top 20 by timestamp desc'
    visualization: 'table'
  }
  {
    name: 'worker-heartbeat'
    title: 'Alert worker heartbeat (last seen)'
    query: 'traces | where tostring(customDimensions.pms_event) == "WORKER_HEARTBEAT" | summarize last_heartbeat = max(timestamp), heartbeats = count()'
    visualization: 'table'
  }
  {
    name: 'errors'
    title: 'Exceptions by type (24 h)'
    query: 'exceptions | summarize count() by type, bin(timestamp, 1h) | order by timestamp desc'
    visualization: 'table'
  }
  {
    name: 'requests'
    title: 'HTTP requests: count and p95 duration by operation (24 h)'
    query: 'requests | summarize requests = count(), failed = countif(success == false), p95_ms = percentile(duration, 95) by name | order by requests desc'
    visualization: 'table'
  }
]

var workbookIntro = [
  {
    type: 1
    name: 'intro'
    content: {
      json: '## PMS agent demo – operations overview\nSynthetic data only. The demo illustrates post-market surveillance workflows and makes no compliance claim. Every AI output is a draft; only the medical reviewer decides.\n\nTraces contain prompt content (synthetic): restrict read access to operators.'
    }
  }
]

var workbookQueryItems = [for item in workbookQueries: {
  type: 3
  name: item.name
  content: {
    version: 'KqlItem/1.0'
    query: item.query
    size: 0
    title: item.title
    timeContext: {
      durationMs: day
    }
    queryType: 0
    resourceType: 'microsoft.insights/components'
    visualization: item.visualization
  }
}]

var workbookContent = {
  version: 'Notebook/1.0'
  items: concat(workbookIntro, workbookQueryItems)
  isLocked: false
  fallbackResourceIds: [
    appInsightsId
  ]
}

resource workbook 'Microsoft.Insights/workbooks@2022-04-01' = {
  name: guid(resourceGroup().id, 'pms-operations-workbook')
  location: location
  tags: tags
  kind: 'shared'
  properties: {
    displayName: 'PMS agent demo – operations'
    category: 'workbook'
    sourceId: appInsightsId
    version: 'Notebook/1.0'
    serializedData: string(workbookContent)
  }
}

output actionGroupId string = actionGroup.id
output workbookId string = workbook.id
output alertRuleNames array = [for definition in alertDefinitions: '${namePrefix}-${definition.key}']
