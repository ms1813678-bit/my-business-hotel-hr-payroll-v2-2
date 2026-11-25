# Hosted PostgreSQL client compatibility correction

25 November 2026. Opening checkpoint3fdd47e passed the complete default inventory locally in13 disjoint runs. That is not hosted CI acceptance.

The subsequent hosted Linux job failed one populated UID dump/restore test after2544 passed and46 skipped in the first shard. The second14-node return-cycle shard was not reached. Migration succeeded. The exact restore command failed because the server rejected the emitted transaction_timeout setting. The log did not print client versions, so no exact client major is asserted.

Evidence: https://github.com/ms1813678-bit/my-business-hotel-hr-payroll-v2-2/actions/runs/45676758377/job/139426227804

The Linux workflow now selects the PostgreSQL16 client directory to match its postgres:16 service. If missing, it installs postgresql-client-16. Both pg_dump and pg_restore must report major16 before adding that directory to GITHUB_PATH and continuing to migration/full inventory. A separate following step logs and verifies bare-command resolution and both effective major versions after GITHUB_PATH takes effect. Shell failure is fatal. No SQL filtering, ignored restore errors, skip or admission change. Windows already uses the same selected installation for cluster and client tools and is unchanged.

Local qualification:27 existing UID roundtrip/CI tests passed onPython3.11.18/PG16.2, including the populated actual four-entity dump/restore and behavioral enforcement. With the new source-contract regression,20 CI tests passed; counts overlap and are not added. Actual both client version outputs matched16.2. This is not hosted rerun success or apt availability proof. Publication triggers the existing workflow, not a manual rerun.

The product backup tool finder also uses PATH first and has no server-major selection. That is a concrete compatibility lead for separate repair/qualification, not fixed by this workflow-only change. There is no evidence here of a lost real backup or hotel data. Windows completion/native startup cause and actual hotel-device acceptance remain separate.
