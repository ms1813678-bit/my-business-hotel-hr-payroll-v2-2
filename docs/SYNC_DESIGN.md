# Three-device sync: design and test plan (draft, nothing built yet)

Status: design note, 27 Nov 2026. It describes what will be built and how it will be proven. It is not a claim that sync exists. Today only identity primitives exist (`sync_uid` on four tables, a read-only snapshot, per-role column visibility). No transport, conflict handling or applier is in the product. The append-only change log table and its module exist since migration 0084 (section 8), unused by any screen.

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

## 7. Questions for Mo (answered 2026-11-27 18:57)

Mo answered "نعم" (yes) to both proposals below on WhatsApp at 2026-11-27 18:57:17 (wamid.HBgMMjAxNTAwMDY5Mzc1FQIAEhggQTVDNTJGQjI1RTAzNzU3NkM2NURBRTQ5RkIwMUMyNkUA), as a reply to the two questions the coordinator asked him; the one-word reply is read as yes to both. These are now the design.

- Decided: device credentials use a one-time pairing code shown on the main device and typed on the new device.
- Decided: LAN access. The app is bound to this PC only today. Sync needs the main device to accept connections from the two other PCs on the hotel network. LAN access is off by default, turned on by the administrator in settings, and only for the sync address.

## 8. Sync core, part 1a: what exists now (change log only)

Added: migration 0084 `sync_change_log`, `app/sync_change_log.py`, `tests/test_sync_change_log.py`. Nothing writes to the log yet (no screen or service calls it) and nothing reads it except tests, so no behavior of the product changes. This is not sync. It is the record sync will be built on.

- One row is one field change: device id, a per-device counter starting at 1, entity, uid, field, old and new value as snapshot_codec text, a canonical UTC time, the previous row's hash and its own hash. Markers `create` and `delete` use field `*`.
- Canonical time (from the harness finding 4): `YYYY-MM-DDTHH:MM:SS.ffffffZ`, UTC, fixed width. A naive time is refused.
- The database keeps the table append-only: no delete, no truncate, no edit. The one allowed update is the main device giving `main_seq` to a row once (NULL to a number).
- Parent references are logged as `<x>_uid` with the parent's uid, never an integer id. `branch_id` is refused until branches have a uid (open item below).
- The export carries the new value only. `old_value` stays in the log for the audit trail and is never exported, so a value a role may not see cannot leak through the old side of a change. `app/sync_device_export.py` then filters rows by role. Its `validate()` answers "may this role read this field", not "may it edit it"; edit rights per role are a separate rule nobody has set, so the applier must not use `validate()` for writes.
- The leave reason, cancellation reason and actor names are readable by all three roles in the current column lists (they are not assistant-only).

Proposals for the next parts (technical items 1 to 3 need no business answer; nobody has approved them yet):
1. Reference rows created by migrations (for example the leave type "annual") get a different random uid in every install. The sync uid of such a row will be derived from a fixed namespace and its natural key, so all installs agree. This needs a migration step and comes with part 1b.
2. `branch_name` is derived on the main device when exported. A device keeps it in a read-only, sync-managed cache keyed by branch uid. Branch renames are sent as new `branch_name` values.
3. The first full-state sync starts the device at the main's current `main_seq`, never at 0.

Mo's 27 Nov 21:22 voice note (wamid.HBgMMjAxNTAwMDY5Mzc1FQIAEhggQTUxQkY5NjREREIzNDUxQUJERTBGMTM5NDM3NDUyODAA), as the coordinator relayed and I checked in the channel: the main device enters the initial data, then it spreads to the other devices; each device sees only what it is allowed to see; he leaves the distribution to us. It is not a literal yes to "employees, departments and leave only on the other PCs", so the first-sync scope below is a proposal that follows from that note.

Proposed first sync scope (not decided by Mo in these words): employees, departments, leave types and leave records go to the other devices, each role filtered; payroll runs, advances and attendance reports stay on the main device.

Built in part 1b-i (`app/sync_apply.py`, pure, not wired): the decision step for one session, as described in its module docstring. It takes an explicit edit policy and has no default, because edit rights per role are an open item below.

Open items (not decided, not built):
- Edit rights per role: which role may change which field. Nobody has decided this. `validate()` is read visibility only and must not be used for writes.
- Rule 6 after Mo's voice note of 27 Nov 22:27 (wamid.HBgMMjAxNTAwMDY5Mzc1FQIAEhggQTU3MUNBNTQ5MTY2MTFDQTYzQkMyOUZCNEIyMUNCREYA). In meaning, not as a literal A or B choice: the sync data is not thrown away and re-entered from scratch. It goes back to the administrator who entered it, with a warning that the action he did is wrong and he must remove it so the sync can continue. The whole sync stays stopped (nothing half-applied) until then.
  Technical proposals for the edge cases, not decided by Mo: (a) the stopped session is kept as "returned" with the name of the offending change; (b) it is kept until the administrator removes the change, and the main device shows it as a pending warning; (c) if that administrator cannot be reached, the main administrator may remove the offending change or discard the session; (d) a returned session is never applied in part; (e) a returned session older than a set number of days (proposal: 30) is shown as expired and needs a decision.
- Base version per change: two edits of one field in a row from the same device must not look like a conflict. The log has the old value and the counter; the apply part must compare the old value with the main's current value.
- Wall-clock time can drift between machines. The log keeps both the counter and the time; order decisions use the counter and `main_seq`, the time is shown to people only.
- Branches have no uid, so branch_id changes cannot be logged yet.
- Delete and create markers are logged; `sync_device_export` drops field `*` today, so deletes do not reach devices until part 1b handles markers.
- Pairing code lifetime: proposal 10 minutes, single use. To ask Mo with the other sync points.
- Offline edits that were made before a restore of the main, and edits to a not-yet-completed employee, need a rule in part 1b.

## 9. Technical proposals in part 1b-ii (not decided by Mo, not wired)

- Seeded uids (`app/sync_uid_seed.py`): only reference rows made by migrations (leave types, key = code) get a uid derived from a fixed namespace and the natural key. Departments (user-entered, so two installs could collide silently) and branches (no uid yet) have derivation helpers marked as proposals; they are refused unless the caller passes `proposal=True`. Applying any seeded uid to existing rows needs a migration that does not exist yet.
- A delete of a record that is already deleted (on main, or earlier in the same session) is a no-op listed in `SessionPlan.already_deleted`, not a conflict. Technical proposal; no business rule behind it.
- Rows in a session are sorted by counter before checking; an identical repeat is kept once, the same counter with other content is an integrity failure.
- `app/sync_apply_ops.py` fixes the shape of the ops list and of the returned-session record, and `is_expired` takes the number of days as a required argument; the 30-day figure above stays a proposal.
- The next part (the executor) must record consumed conflict and already-deleted rows in the main's change log, so the device's counter chain continues without a gap.

## 10. Technical proposals in `app/sync_rules.py` (pure, not wired, not decided by Mo)

- Restore guard (rule 9): the device sends the last counter it believes main holds (its last synced counter, not its latest local one, which pending rows push ahead legitimately). Refused when the device epoch is newer than main's, when the device's last counter is ahead of main's record, or when the device epoch is older than main's (its pre-restore edits wait for an administrator decision; that last refusal is a proposal for the open item above).
- Administrator decision on a conflict: accept turns a same-field conflict into the device's set and a delete-against-edit into the delete; reject changes nothing. Accepting an edit or create on a deleted record is refused until a rule for undelete exists. "Confirm both" for finance salary is not built.
- Duplicate employee code (rule 7): codes compare by number. A free code is kept and, if its number is above the counter, the counter advances to it; a taken one becomes the next number after the highest of the counter and all codes in use. The function returns the counter value the caller must reach, in the same transaction.
- Executor note: an accepted same-field conflict carries the device's old value while main holds another; the executor applies it as decided and does not flag it as a new conflict.
