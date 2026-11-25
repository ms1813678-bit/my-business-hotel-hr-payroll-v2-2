# Migration logging and source-test encoding correction

Authenticated8526380Linux job139439329019 completed client selection and migrations, then its first full shard ended3failed,2776passed,46skipped in487.96seconds. The only failures were the three existing AI configuration warning assertions. The563329eWindows job139435730102 showed the same three warning failures plus one source-text UTF8/default-cp1252 decode failure and48 distinct known unsupported operational-parser failures. These findings do not establish Windows parser support or complete hosted CI success.

The root cause reproduced locally is Alembic environment fileConfig using its default disable_existing_loggers=True. An earlier in-process migration disables the already-created app.ai_local_config logger. The fix passes disable_existing_loggers=False. Migration logger levels/console configuration are unchanged; existing handler reconfiguration is not new. No AI warning implementation or assertLogs expectation is changed and no test-only logger reset is adopted. Existing warning bodies continue to exclude secret/source values.

A real fresh PostgreSQL schema migration regression verifies the existing AI logger remains enabled and emits a value-free warning afterward. The old fileConfig counterfactual fails the logger-disabled assertion. The new test proves this logger, not every application module. Two existing leave draft source checks explicitly read UTF8; their assertions are unchanged.

Isolated sequence:44passed,13warnings,10.94seconds with actual AI HTTP in-process migrations before original configuration warning tests, the new regression and draft tests. This is not a full suite or Windows execution. Independent focused static source review accepted the root fix with these limits.

Observed job sources:
- https://github.com/ms1813678-bit/my-business-hotel-hr-payroll-v2-2/actions/runs/45680462056/job/139439329019
- https://github.com/ms1813678-bit/my-business-hotel-hr-payroll-v2-2/actions/runs/45679438529/job/139435730102

Exact integrated shared focused verification:65passed,0skipped,13warnings,10.85seconds, Python3.11.18 with migratedPG16.2. No per-commit progress credit for this corrective batch.
