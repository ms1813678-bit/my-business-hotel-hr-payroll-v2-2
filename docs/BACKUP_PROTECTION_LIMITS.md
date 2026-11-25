# Backup protection and deployment checks

Source checkpoint: cabb5c9195313ed329acaa1a3b9977fe2ff5e42b. This records current behavior and open installation checks. It does not change encryption, scheduling, file permissions or Windows task identity.

## What encrypted archives protect

New backups require the separately configured backup passphrase. Archives use AES-256-GCM with a random data key. The live database stores that working key so scheduled backups can run without a person entering the passphrase each time. The exported key record removes the working key and contains its scrypt-wrapped form. A damaged encryption record refuses a new backup rather than writing an unencrypted archive.

An archive and its exported key record taken without the live database still require the passphrase, subject to offline passphrase guessing. This is not protection against access to the live database, the application's database credentials, an administrator/SYSTEM process, or the whole unlocked device. Anyone with the live database can obtain both ordinary records and the working backup key. Passphrase wrapping does not provide whole-device disk encryption. Existing unencrypted archives are not made encrypted retroactively or deleted automatically.

The owner's recorded choice was a separate passphrase entered in the application and kept away from the hotel device, with mandatory encryption for new archives. It did not promise that a compromised live device could not read its own data. No encryption redesign or manual-only backup policy is introduced by this document.

## Windows scheduled task boundary

The current installer configures scheduled backups as SYSTEM with highest run level, invoking Python and code in the project directory. No installer-enforced ACL protection for project/venv/import/script/.env paths has been established. If an untrusted local account can replace code or an interpreter used by that task, the task could execute modified code with elevated rights.

Actual hotel-device ACLs, owners, inherited permissions, replacement rights on parent directories, reparse targets, interpreter/import search paths, task principal/run level and the backup drive's required permissions are not yet verified. Merely checking a few broad Windows groups is not an effective-rights proof. A proposed preflight classifier with canned fixtures is not production enforcement or native Windows acceptance. No risk-acceptance bypass, ACL mutation or task-account change is approved by this record.

Before accepting unattended scheduled operation, verify effective trust of every execution/dependency path and necessary operator/backup-drive rights on the actual installation. Unsafe or unknown path trust remains a deployment acceptance blocker. This document makes no claim that hotel data, Windows permissions or live scheduled tasks have been inspected.
