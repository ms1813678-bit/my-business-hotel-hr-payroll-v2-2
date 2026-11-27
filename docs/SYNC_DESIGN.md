# Three-device sync: design and test plan (draft, nothing built yet)

Status: design note, 27 Nov 2026. It describes what will be built and how it will be proven. It is not a claim that sync exists. Today only identity primitives exist (`sync_uid` on four tables, a read-only snapshot, per-role column visibility). No transport, change log, conflict handling or applier is in the product.

Sources: Mo's decisions of 23 to 27 Nov (the desktop-app decision record; his 27 Nov 04:47 to 04:49 answers behind rules 4, 7, 8, 9 and 10; and his 27 Nov 17:29 instruction that sync is tested by us in a sandbox and on GitHub, not postponed for lack of hotel PCs). Where this note and the project decisions notebook differ, the notebook and Mo's own messages win and this note changes.

## 1. Roles and shape

- One main device (the HR manager's PC). It owns the truth. Two other devices (the assistant manager, the finance manager) work offline and sync when they can reach the main device on the hotel network.
- The main device does not have to stay on. Other devices never talk to each other, only to the main device.
- Each device has its own local database. Sync moves changes, never whole databases.

## 2. Rules from Mo (each is a test below)

1. Different records changed on different devices merge silently.
2. Different fields of the same employee changed on two devices (for example name on one, salary on the other) both apply automatically.
3. The same field of the same record changed on two devices is a conflict. Both versions are shown with who changed it and when. The other device's change waits for the main administrator to accept or reject. Everything else keeps syncing meanwhile. For finance salary changes that overlap a newer main change, the administrator sees both values with dates and chooses to confirm both or accept one.
4. Delete on one device and edit on another goes to the conflict list. Nothing is deleted silently.
5. Data a role may not see never reaches that role's device (the per-role column lists in `app/sync_visibility.py`). An edit from a device to a field its role cannot see is rejected and the administrator is alerted.
6. All or nothing: if a sync is interrupted (network, power cut, any error) the whole sync is cancelled and redone from the start. No half-applied sync is ever left behind.
7. Duplicate employee code created offline: the main device gives the next sequential code. The offline device receives the new code and its local row is corrected.
8. A new employee created on an assistant device stays pending as incomplete, with an alert to the administrator, until the administrator completes it.
9. Main restored from an older backup: sync is refused until the administrator decides. Devices that are ahead of the restored main must not silently overwrite it or be overwritten.
10. The main device shows when it received each change.

## 3. Mechanism (proposed, to be confirmed in review)

- Identity: `sync_uid` already exists on departments, employees, leave types and leave records. Other tables join the sync only after their own contract and migration.
- Change log on every device: an append-only table of changes (entity, uid, field, old value, new value, device id, local time, a per-device counter, a hash chain for tamper evidence). Written in the same transaction as the business change.
- Per-device state on the main device: the last counter received from each device, so a retry never applies a change twice.
- Session: a device opens a sync session with the main device over HTTP on the hotel network (localhost-only binding stays the default; LAN binding is an explicit, administrator-enabled setting, with TLS or a pinned-key check to be decided before it is turned on). The device authenticates with its own device credential and its account role. The main device checks the role before sending or accepting anything.
- Apply on the main device: one database transaction per session. Validate every change (role may write this field, uid exists, value passes the same checks as the screens). Auto-merge when the rules above say so. Otherwise write the change to a conflict table and leave the business row unchanged. If anything raises, roll the whole session back and report a failed sync (rule 6).
- Return path: the main device sends, in one response, the changes the device is allowed to see since its last counter, plus any code corrections (rule 7). The device applies them in one transaction and then confirms; the main device records the confirmation.
- Restore guard: a restore epoch number stored with the database and sent in every session. A device whose epoch is newer than the main's, or whose last counter is ahead of the main's record for it, is refused with an explicit message (rule 9).

## 4. What a screen change needs

The conflict list, the incomplete-employee alert and the restore-refusal notice are new screens or banners. Mockups go to Main first, in Arabic first with English and Chinese text, before any template is written.

## 5. Proving it without hotel PCs

Test harness: start one main instance and two device instances as separate processes, each with its own PostgreSQL database and port, on one machine, talking over localhost. The same scenario runs on a Linux runner and on a Windows runner.

Scenarios (each has an expected final state and a data-loss check that compares row counts and a content hash across all three databases after sync):

- two-way edits on different records; different fields of one record; same field (conflict, then accept; then reject);
- delete versus edit; edit versus edit with the delete accepted;
- offline: stop a device process, change data on the main and the device, restart, reconnect, converge;
- interrupted sync at each step (kill the device process before send, during receive, before confirm; kill the main during apply): after retry the state equals a clean run, and no partial rows exist;
- duplicate employee code from two offline devices;
- role visibility: the finance device never receives assistant-only data and the assistant device never receives salary; a forged edit of a hidden field is rejected and alerts;
- restore the main from an older backup, then try to sync: refused;
- replay: sending the same session twice changes nothing;
- randomized runs: a seeded generator produces hundreds of operation sequences across the three instances with random disconnects, and checks the invariants (no lost accepted change, no silent overwrite, all databases equal after a final clean sync). The seed is printed so a failure can be repeated.

What CI can prove: the logic, the transaction behavior, process crashes, retries, role filtering and restore refusal, on Linux and on Windows processes and paths.

What needs the real hotel PCs: real network behavior (Wi-Fi, router and Windows Firewall prompts, sleeping laptops, address changes), clock differences between machines, antivirus interference, real disks, and real users. That stays the last step, outside every progress figure.

## 6. Build order (each item is a separate reviewed change)

1. This note, reviewed. 2. Change log and per-device counters on every device. 3. Main-side session apply with the merge rules and the conflict table, tested alone. 4. HTTP session and device credentials. 5. Device-side pull and apply. 6. Screens (after mockups). 7. The multi-instance CI harness with the scenarios above, growing alongside items 3 to 5, not after them.

## 7. Open questions for Mo (plain)

- Device credentials: a one-time pairing code shown on the main device and typed on the new device. Recommended.
- LAN access: the app is bound to this PC only today. Sync needs the main device to accept connections from the two other PCs on the hotel network. Recommended: off by default, turned on by the administrator in settings, and only for the sync address.
