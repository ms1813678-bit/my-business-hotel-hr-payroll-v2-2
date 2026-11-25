# Historical Muse review reconciliation

Updated 25 November 2026 against published login qualification 5a3b62d and the separately qualified XLSX candidate. This is a bounded disposition record, not a new business policy or a claim that the entire project was re-audited.

## Coverage and provenance

The historical review concerns f1bb989. A separate read-only reviewer computed that its manifest contains the same 642 files and 100,962 per-file lines as that tree. Exact bytes actually delivered to the reviewing service and a complete line-by-line read remain unverified. The service reported exclusions/redactions and claimed broad coverage; those are claims, not established coverage. Peer citation spot checks and a further roughly fifteen-site source check do not prove every reported finding. Historical source hashes must not be treated as hashes of the current tree.

Current monthly cabb5c9 changes more than a chart branch: employee-period membership, status/history reporting, missing/comparison states, attendance chart semantics, PDF layout, translations, tests and evidence were separately qualified. Login 5a3b62d adds evidence/tests, not a login-policy change. No historical report should be presented as review of these newer changes.

## Alleged high findings

| Finding | Disposition and boundary |
|---|---|
| Assistant leave types, balances and roster access | Settled by recovered original user approval. The 22 November 20:19-20:20 exchange explicitly added this scope to the assistant. Ordinary leave/balance writes are not an accidental authorization defect. This does not decide other employee, salary, advance or staff-action permissions and columns. |
| Login counter missing row lock | No ordinary HTTP failure-counter bypass established. The route already acquires the exclusive transaction advisory gate before the counter read/authentication/failure update. Actual fully migrated PG HTTP/CSRF tests observe the second backend advisory wait and persisted two-failure/two-hour lock with one audit. Standalone helper unlocked read/update is a source observation, not an executed bypass. Gate-disabled experimental tests fail their wait assertion, not a proved lost-update schedule. Success/restore/other helper interleavings remain outside this proof. Original messages establish two attempts and two hours. Missing scope in those particular messages alone does not create a new global/per-IP user question; recover full original scope before any proposal. |
| Backup working data key stored in the live DB | Confirmed protection boundary, not newly repaired. Unattended backups use a live working key; exported key records omit it. Archive plus exported record requires passphrase, but live DB access can expose data and working key. See BACKUP_PROTECTION_LIMITS.md. No crypto/task-account/ACL redesign is authorized by this finding. |
| Old leave confirmation permits negative carry | Existing accepted negative carry differs from prohibited new manual negative opening. Recovered original 23 November 23:23-23:25 exchange supports this distinction. No overdraft-limit approval inferred. |

## Other reported findings

| Finding | Current disposition |
|---|---|
| Nonfinite money | Separate reviewer executed current FastAPI/pydantic form checks rejecting NaN/Infinity variants with 422. Internal helper guards and extreme finite values remain a separate lead; no whole-money-path safety claim. Dependency-sensitive evidence, not an immutable framework guarantee. |
| XLSX formula inference | Confirmed and minimally fixed in the candidate qualified by the integrating writer. Both export writers emit values, no intentional formulas. Final-save normalization turns only inferred formula cells to text. Six AR/EN/ZH actual migrated-PG exports preserve literal values; focused 155 tests pass. See XLSX_FORMULA_SAFETY_1699.md for publication state and limits. |
| National ID default fields / further role permissions | Separate unresolved B8 scope. Existing assistant field prohibitions remain enforced. Recover the applicable user originals before presenting business choices. Do not reopen already approved ordinary leave scope. |
| AI memory retention | Historical claim references a retention-pinning test. This reconciliation did not re-audit retention implementation or recover full owner instructions. Not a newly decided deletion policy. |
| SYSTEM scheduled execution from project paths | Source confirms SYSTEM/highest tasks using project interpreter/scripts. Actual Windows effective rights, path trust, ancestors, reparse points, interpreter/import/.env writes and task configuration are unverified. Deployment acceptance blocker, not proof of an installed exploit or secure installation. A synthetic classifier/spec is design evidence only and is not adopted into the installer. |
| Migration 0035 downgrade account removal | Separately source-confirmed destructive rollback design, not a routine application path. No downgrade executed or changed here. |
| Translation overrides | Separate AST count found 104 later differing assignments at historical/current checked bases. Final-value ordering exists, but correctness of every translation was not reviewed. No mass rewrite. |
| Workflow fetch depth | Unverified historical claim. No CI-green statement. |
| Historical hash mismatches | Requires fresh exact-source measurement in any new handoff. Historical report counts cannot describe current publication. |
| Installment 25 percent cap | Source differentiates regular advances from installments and a test pins the distinction. The historical reviewer did not search user originals. Original-scope recovery is required before calling this a new user decision or changing the rule. |

## Handoff and acceptance

No native Windows/hotel installation, actual ACL acceptance or full end-to-end delivery is proved here. Earlier delta archives are stale after newer publications and must not be uploaded as current. Refresh a minimal affected-source packet once, after reconciling current source and requirements, and require acknowledgment of its actual contents before nonoverlapping follow-up. Do not imply the historical review covered newer code or use it to authorize actions.
