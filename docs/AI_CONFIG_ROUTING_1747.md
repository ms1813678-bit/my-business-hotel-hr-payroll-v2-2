# Local AI configuration and bounded command routing

25 November2026. Independent source:7fa40bd. No model default, business authority or approval gateway change.

## Configuration

The assistant now reads the installed app's .env as well as process variables. Entire environment source wins over the file, so environment OLLAMA_MODEL wins over file LOCAL_AI_MODEL; inside either source LOCAL_AI_MODEL wins over its documented alias. Blank values fall through. An invalid winning model name logs a warning and uses the unchanged default. Values are read at import; restart after edits. UTF-8 with or without a BOM is supported. A missing reader or unreadable file logs a fixed warning without configuration values. Missing file is ordinary absence.

python-dotenv is a direct dependency; it was also present through pydantic-settings in the qualified environment. Windows setup installs project dependencies and the installer includes pyproject.toml. This is source/dependency evidence, not an actual Windows install. Existing status, planning and chat use the same resolved constants. No automatic pull is added. Existing pinned loopback, no proxy/redirect, TLS and authentication rules remain.

Current default qwen2.5:3b is unchanged. It is not production approved; its Research license and production suitability remain unresolved. The separately measured1.5b model was not qualified for production quality/latency, and no7b/hotel-device result is asserted. Three-language documentation removes unmeasured hardware-suitability claims.

## Routing, not execution

Polite AR/EN/ZH forms and bounded everyday correction/add/delete words can reach the existing confirm-first gateway. New everyday verbs require an HR object. Initial-clause text/explanation objects remain read-only, including where the HR noun precedes explanation; a later request for an explanation/preview does not suppress a clear HR-record change. The Chinese ba construction requires a change predicate rather than a change-history noun. Existing explicit execution prefixes and wants_execution are unchanged.

This remains a bounded lexical detector, not general intent understanding. Existing verb behavior is preserved. Ambiguous wording outside the qualified corpus is not certified. Some noun-like read starters, such as English register-of-balances or Chinese registration/correction-history questions, can still enter proposal preparation. They do not execute without review/approval. A blanket question-mark veto was rejected because it also suppressed genuine polite change requests. No user command can bypass current roles, CSRF, source-bound review receipt or explicit approval. Real-model plan interpretation is not established by synthetic model stubs.

## Qualification and provenance

The original config/routing candidates came from the independent qualification worker using distinct external-language corpora. The integrating writer found actual false positives and false negatives, supplied exact probes, rejected two earlier candidates and qualified the narrower third candidate with an additional initial-clause repair. Configuration received independent static acceptance plus BOM/logging/dependency/precedence followups. Static review is not independent pytest execution.

SupportedPython3.11.18, PostgreSQL16.2 plus existingSQLite fixtures: final isolated selection598passed,4existingSQLitevariant skips,48warnings,56.44seconds. Corresponding actualPG cases ran. Includes detector, config, local-only/pinned transport, proposal/confirmation/input/atomicity, actualPGidentifier/selector/HTTP and monthly/numeric characterization. No full inventory rerun is asserted for this independent delta.

Three new actual application HTTP cases use real login, CSRF and current authorization with DB-wiring overrides and deterministic model planning only. Each new polite phrase creates a pending proposal and proposal audit, leaves business record unchanged, refuses execution without reviewed receipt, then confirms once after explicit review/approval. All three fail against the old detector and pass with the candidate. This is not actual-model quality or device acceptance.

The added monthly role tests preserve current source roles: assistant denied payroll/advances/staff actions, attendance detail/chart allowed, finance advances/staff actions allowed. Four actualPG nonfinite/huge advance cases persist nothing. Infinity/huge values reach PostgreSQL numeric rejection; NaN/sNaN stop in Python arithmetic. These characterize current behavior, not an exploit or approved financial-policy change. The authenticated real advance HTTP error path was not established by these cases.

Held attendance0066, journal, native/HTTP experiments, real sync and model-default candidates remain unadopted. main is untouched. Actual hotel-device trial remains last and excluded from development progress percentages.

Final exact shared selection:283passed,zero skipped,5warnings,9.12seconds. All11candidate product/test/docs files byte-matched the qualified isolated candidate before the acceptance-note addition. Independent final narrow static review qualified the bounded delta with no material blocker and the linguistic limits above.
