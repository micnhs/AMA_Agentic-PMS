# Gate C handover checklist

Gate C is the live check of the demo in Azure. Michael, the project owner, runs it himself from the
README and hands the evidence back to the build agent. It covers the Goal Card checks 12 to 14:

| Check | What it shows | Evidence | Concept test |
|---|---|---|---|
| 12 Live deployment from the README | The README deploys the demo into an empty resource group without help | `deploy.log`, `notes.md` | T12 |
| 13 Live smoke test | The deployed demo passes the automated live checks | `smoke-test-result-part1.json` (copy of the part-1 result) | T13 |
| 14 Live human check | A person approves, rejects and edits in the live app, and the audit trail shows it | `smoke-test-result.json` (part 2), screenshot | T14 |

The concept also lists T22 (source fidelity), T28 (cost view in Azure), T30 (evaluation with the real
models) and T32 (Entra sign-in with roles) for Gate C. If the smoke-test result does not show them, write
what you saw in `notes.md`.

Plan about 1 to 2 hours (most of it waiting for provisioning). Costs: the base load of roughly 95 to
110 USD per month starts with the deployment, plus model usage (see the README cost section, dated
10 Nov 2026). Budget notifications go to `DEPLOYER_EMAIL`.

## Screenshots for checks 4 and 8 (CI on GitHub)

The sandbox that built this repository has no browser, so the Gate B screenshots come from the CI job
*UI tests in a browser (Playwright, screenshots)* (decision of the project owner, 11 Nov 2026):

- [ ] Create a **private** GitHub repository and push the unchanged repository content to its `main` branch
      (the CI workflow runs on every push to `main`; it needs no secret and no Azure sign-in).
- [ ] Wait for the workflow *CI* to finish. Download the artifact `ui-screenshots` and, if present,
      `golden-summary`, `arm-templates` and `psrule-report` (they also answer OI-039 and OI-040).
- [ ] Note the workflow result per job (passed/failed) and copy any failure message into `notes.md`.
- [ ] Put the artifacts into `input/handover/<YYYY-MM-DD>/ci/` (see section 8).

## Before you start

- [ ] An Azure subscription where you are **Owner** (or **Contributor plus User Access Administrator**).
- [ ] You may create Microsoft Entra app registrations, **or** an admin gives you the client ID of a
      prepared registration (README: *Bring your own app registration*).
- [ ] Model quota for `gpt-6-astra` and `gpt-6.1-sol` as **Data Zone Standard** in the chosen EU region
      (default Sweden Central, 50 thousand tokens per minute each). The preflight checks it.
- [ ] Tools: Azure CLI 2.67 or newer, Azure Developer CLI 1.18 or newer, Python 3.12 or newer, bash
      (on Windows: PowerShell 7.2 or newer for the `.ps1` scripts, started with `pwsh`; Windows PowerShell
      5.1 is not supported).
- [ ] An unchanged copy of the repository: a fresh `git clone` or the delivered zip, unpacked into a new,
      empty folder. Prefer a folder outside OneDrive (for example `C:\src`). For a downloaded zip on
      Windows, unblock the files first (README, *Offline quick start*, step 1).
- [ ] A demo mailbox for critical-case e-mails (your own address is fine; never a real third party).

## Steps

### 1. Start from a clean state

- [ ] Open a new terminal in the repository folder (the folder with `README.md`).
- [ ] Choose the azd environment name. The README uses `pms-demo`; keep it unless a deployment with that
      name existed in this subscription during the last 7 days (its Key Vault name is still reserved by
      purge protection and the preflight would stop). Then pick a new name, for example `pms-gatec`,
      and use it in every command below.
- [ ] The deployment creates the resource group `rg-<environment name>` itself, so it starts empty.
      Check that it does not exist yet: `az group exists --name rg-pms-demo` must print `false`.

### 2. Deploy word for word from the README

Follow the README sections *Prerequisites* and *Deploy to Azure* **word for word**:

- [ ] Step 1: `az login`, `azd auth login`, `azd env new ...`, `azd env set DEPLOYER_EMAIL ...`.
- [ ] Step 2: the human check needs the roles analyst and medical reviewer, so set all roles **before**
      you deploy:
      `azd env set DEPLOYER_APP_ROLES PMS.Operator,PMS.Analyst,PMS.MedicalReviewer,PMS.Regulatory`.
- [ ] Step 3: `bash scripts/preflight.sh`.
- [ ] Step 4: deploy and record the console output in `deploy.log`:

```bash
bash scripts/deploy.sh -e pms-demo 2>&1 | tee deploy.log
```

PowerShell:

```powershell
./scripts/deploy.ps1 -EnvironmentName pms-demo *>&1 | Tee-Object -FilePath deploy.log
```

- [ ] The run ends with `[pms] Deployed. Web app: https://...` and the postdeploy hook reports
      `Web app is healthy`.
- [ ] The postdeploy hook may warn `Index not updated from here ...` or `Agents not published from here ...`
      (for example when the Azure Python packages are not installed on your machine). That is expected:
      the worker container updates the search index and publishes changed agents when it starts. Note
      the warning in `notes.md`.
- [ ] If a step fails, do not work around it silently: copy the `FAILED: ... Fix: ...` line into
      `notes.md`, apply the fix it names, and run the step again (append with `tee -a deploy.log`).
      If piping through `tee` causes trouble, run the command without it and paste the console output
      into `deploy.log` afterwards.

### 3. Sign in with your roles

- [ ] Check the postprovision output: it lists the app roles assigned to you.
- [ ] Run `az login` again, so that new tokens contain the roles.
- [ ] Open the web app (`azd env get-value WEB_URL`) and sign in with Microsoft Entra ID. If you were
      signed in before the role assignment, sign out and in again.
- [ ] Open `<WEB_URL>/api/me`: it shows your user and the PMS roles the app sees in your token.
- [ ] Optional: open the web app in a private browser window with an account **without** a PMS role;
      the sign-in must be refused (Entra-only sign-in, roles from the token).

### 4. Load the demo data

- [ ] `bash scripts/seed.sh --azure 2>&1 | tee -a deploy.log`
      (PowerShell: `./scripts/seed.ps1 -Azure`). Needs the role `PMS.Operator`.

### 5. Smoke test, part 1 (automated)

- [ ] `bash scripts/smoke-test.sh --part 1` (PowerShell: `./scripts/smoke-test.ps1 -Part 1`). Needs the
      role `PMS.Analyst`. It uploads a synthetic critical smoke-test letter (a new fictional case with
      patient `P-ZZ-...`) and waits up to 10 minutes for the analysis.
- [ ] Every check prints `PASS`, `FAIL` or `SKIP`; the last line says `passed` or `FAILED`. With the
      role `PMS.MedicalReviewer` in your token, the check "decision refused for non-reviewers" is skipped
      (by design). Note the result in `notes.md`.
- [ ] **Keep the part-1 result**, because part 2 replaces the file:
      `cp smoke-test-result.json smoke-test-result-part1.json`
      (PowerShell: `Copy-Item smoke-test-result.json smoke-test-result-part1.json`).

### 6. Human check in the browser

Use three **different** cases. A critical case first shows *Triage required*: take over the triage (analyst
or medical reviewer); when the analysis has finished, the case shows *Draft ready*. Cases that are not
critical show *Draft ready* directly.

- [ ] **Approve the smoke case** as medical reviewer – the case whose ID is in `smoke_case_id` of the
      part-1 result: take over the triage, then the review, then approve it with a reason. Part 2 checks
      exactly this case. Do not change the smoke case afterwards: any change voids the approval.
- [ ] **Reject** a second case: choose a reason code (R1 to R6) and write a reason. It appears in the
      *Rejected* view.
- [ ] **Edit one metadata field** of a third case with a reason. The audit history shows your user, the old
      and the new value.
- [ ] Take **one screenshot** that shows the result (for example the case view with the decision and the
      audit history, or the dashboard). Save it as `screenshot-1.png`.
- [ ] Write the three case IDs into `notes.md`.

### 7. Smoke test, part 2

- [ ] `bash scripts/smoke-test.sh --part 2` (PowerShell: `./scripts/smoke-test.ps1 -Part 2`).
      Part 2 reads the smoke case ID from `smoke-test-result.json`, checks the human decision with its
      reason, the approval bound to a content hash, that no agent or system decided, that the case report
      shows the reviewer decision and that the audit chain verifies, and then writes its own result to
      `smoke-test-result.json`.
- [ ] Optional: `bash scripts/verify-audit.sh --azure` (the audit chain must verify).
- [ ] Optional: look at the *Costs* and *Governance* views and the workbook *PMS agent demo – operations*
      in the Azure portal; note what you see.

### 8. Hand over the evidence

Create the folder `input/handover/<YYYY-MM-DD>/` in the project workspace (the input folder you share
with the build agent – **not** in this repository) and copy into it:

- [ ] `deploy.log`
- [ ] `smoke-test-result-part1.json` (part 1) and `smoke-test-result.json` (part 2)
- [ ] `notes.md` (template below)
- [ ] `screenshot-1.png` (and any further screenshots)
- [ ] `ci/` with the downloaded CI artifacts (`ui-screenshots`, optionally `golden-summary`, `arm-templates`,
      `psrule-report`) and the job results in `notes.md`
- [ ] Optional: a newer repository zip, if you changed anything in the repository

Before you copy: read through the files once. The scripts never print tokens or keys; `deploy.log`
contains resource names, IDs, URLs and your e-mail address.

### 9. Afterwards

- [ ] Keep the deployment for presentations, **or** remove it: `bash scripts/teardown.sh` and type the
      environment name to confirm (PowerShell: `./scripts/teardown.ps1`). Costs continue until teardown.
- [ ] After a teardown, the Key Vault name stays reserved for 7 days; deploy again with a new environment
      name.

## Known risks of the first live deployment

The infrastructure templates were written in a build workspace without the Bicep CLI and without
network access, so the first `az bicep build` (preflight and CI job `bicep`) and the first `azd provision`
are also their first real test. If one of the following happens, apply the workaround and write it into
`notes.md`.

| Risk | What you would see | Workaround |
|---|---|---|
| A property or type error in a Bicep file | Preflight: `infra/main.bicep does not compile: ...` | Note the first error line; run `az bicep upgrade` and try again. |
| Unknown Microsoft Graph extension version | Bicep build error about the extension `microsoftGraphV1` | See [If the Bicep build fails](#if-the-bicep-build-fails). |
| Content filter or Foundry connection not accepted by the API version used | Provisioning error in the Foundry module (`raiPolicies` or `connections`) | `azd env set CUSTOM_CONTENT_FILTER false` (built-in default filter policy; OI-034), then provision again. |
| A diagnostic setting category group is not supported by a resource type | Provisioning error in a diagnostic setting | Note the resource type and the message. |
| E-mail sending is refused for the app identity | No alert e-mails; authorization error in the worker log | Fallback: give the app's managed identity (`id-pms-...` in the resource group) the role Contributor on the Communication Services resource only. This grants more than needed; note it. |
| Placeholder image after the first provision | Health probes fail for a few minutes | Expected until `azd deploy` has finished; the first image pull can also wait for the AcrPull role to propagate. |
| Model offer or quota names differ in your region (OI-010) | Preflight model or quota check fails | Follow the fix in the message; fallback `MODEL_FALLBACK_ALL_GENERAL=true`. |
| The Azure SDK packages behave differently from the test doubles (the Azure adapters were tested only against fake SDKs) | Errors from `pms.azure.*` in the web or worker log, for example at the search index sync or at a Cosmos DB query | Copy the first error lines from `az containerapp logs show` into `notes.md`. The search adapter falls back to the REST API when the SDK index models do not fit. |

### If the Bicep build fails

The preflight message `infra/main.bicep does not compile` refers to this section. `infra/bicepconfig.json`
pins the Microsoft Graph Bicep extension that creates the app registration:

```json
"microsoftGraphV1": "br:mcr.microsoft.com/bicep/extensions/microsoftgraph/v1.0:1.0.0"
```

1. Run `az bicep upgrade`, then `az bicep build --file infra/main.bicep` to see the full error.
2. If the error names this extension or its version, look up the current version of the Microsoft Graph
   Bicep extension (`v1.0`) in its Microsoft Learn documentation or in the registry tag list
   (<https://mcr.microsoft.com/v2/bicep/extensions/microsoftgraph/v1.0/tags/list>) and replace `1.0.0`
   with it.
3. If you cannot fix the error, write it into `notes.md` and stop – the build agent will fix the
   template. Bringing your own app registration does not help here: the template still declares the
   extension and needs it to compile.

## Template for `notes.md`

```markdown
# Gate C notes – <YYYY-MM-DD>

- Start and end (UTC):
- Repository version (commit or zip file name):
- Environment name, region, subscription type (offer):
- Deployed with: scripts/deploy.sh | scripts/deploy.ps1 | azd up | GitHub Actions
- Own app registration (bring your own): yes / no
- Model fallback (gpt-6.1-sol for all agents): yes / no; quota changes:
- Preflight warnings or skipped checks (PREFLIGHT_SKIP):
- Deviations from the README (step, message, what you did instead):
- Smoke test part 1: result (PASS / FAIL / SKIP per check)
- Human check: approved smoke case ID; rejected case ID and reason code; edited case ID and field
- Smoke test part 2: result
- Observed time from upload to alert e-mail (seconds):
- Observations requested by open issues:
  - OI-013 Entra rights: could the deployment create the app registration?
  - OI-014 Critical cases: did the number of critical cases feel too noisy?
  - OI-029 Cost and run time of the extended analysis (cost view per case):
  - OI-030 Cost Management API: did the cost view show actual costs and a forecast?
  - OI-034 Content filter with Prompt Shields deployed as configured?
- Gate C tests not visible in the smoke-test result (T22, T28, T30, T32), and what you saw:
- Anything else:
```
