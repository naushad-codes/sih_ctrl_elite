# PRATIBIMB Release Candidate Hardening Report

Audit updated: 4 October 2026  
Scope: release-candidate prompt in `633c5c32-9105-4ce9-96e6-c5d80f10d72e/Pasted text.txt`  
Result: the requested hardening implementation and automated demonstration flows are in place. This is a prototype acceptance report, not production certification. A timed, human-performed 3–5 minute presentation rehearsal and live NCPOR reachability check remain outstanding.

## Executive status

- **Automated release checks:** passing.
- **Three role workflows:** exercised separately and in a single browser workflow.
- **Demo seed/reset:** deterministic, admin-gated, isolated through `DEMO-PRATIBIMB-001`; reset preserves non-demo operations and weather.
- **UI browser inspection:** screenshots captured at 768×1024, 1024×768, 1366×768, 1440×900, and 1920×1080 for Command Center; primary HQ, station, and technical views inspected at 1440×900.
- **No known critical or high-severity defects** found in the scoped checks.
- The platform remains a human-led prototype. Browser offline mode is simulated queueing, and prototype credentials are not suitable for deployment.

## Feature matrix

| Area | Status | Evidence and boundary |
|---|---|---|
| Station and role selection | IMPLEMENTED | Maitri/Bharati and HQ, station, technical roles. Browser login exercised for all roles. |
| Authentication, session, logout | IMPLEMENTED | Server-bound session and revocation; shared authenticated top bar displays station, role, sync receipt, user and sign-out. |
| Role authorization and station isolation | IMPLEMENTED | Browser/API checks deny station/technical HQ access, deny unauthorized transitions, and return no Bharati access to Maitri operation. |
| HQ Command Center | IMPLEMENTED | Station-scoped stored-state overview; weather unavailable is explicit; operational emulator values remain labelled demo/synthetic. Redundant in-page station/role brand header removed in favor of shared shell. |
| Digital Twin | IMPLEMENTED WITH PROTOTYPE LIMITS | Reuses read-only dependency visualization. No equipment control; emulator values are synthetic/manual, not calibrated telemetry. |
| Scenario Lab and history | IMPLEMENTED | Browser run of CHP Unavailable created persisted model output and a linked inspection request. Assumptions, projection, constraints and uncertainty remain separately labelled. |
| NCPOR and historical weather | IMPLEMENTED WITH EXTERNAL CHECK PENDING | Official observation is displayed from stored source metadata or reported unavailable; no values are fabricated. Test verifies weather survives demo reset. Live provider reachability was not checked. Historical dataset files were not changed. |
| HQ remote operations | IMPLEMENTED | Create, review/queue, immutable events, linked operation and outcome lifecycle; browser exercise reaches close. |
| Station inbox, observations and reports | IMPLEMENTED | Browser exercise covers delivery, acknowledgement, start, manual observation and station completion report. |
| Offline queue and sync | IMPLEMENTED AS DEMO SIMULATION | Browser-local queue was observed pending while simulated offline and empty/synced after reconnect; report and observation reached backend. Not a durable disconnected edge database; unsynced browser data is not HQ-visible. |
| Idempotency and conflicts | IMPLEMENTED | Backend integration tests cover idempotent replay and revision-conflict resolution; one report event remains after sync. |
| Reconciliation and follow-up | IMPLEMENTED | Browser creates Projection Check, compares stored projection and station report, records a human classification, and closes operation. Follow-up/reopen APIs remain covered by backend tests. |
| Provenance and audit | IMPLEMENTED IN APPLICATION WORKFLOWS | Read-only timeline, source/projection/report metadata, reconciliation provenance chain and audit drawer are present. Full DB-level immutable triggers and a formal tamper-proof audit store are not provided. |
| Technical Engineer | IMPLEMENTED | Browser records manual engineering observation, creates a technical finding, and marks it complete. Finding status is separate from remote-operation lifecycle. A separate workflow test verifies operation status is unchanged. |
| Demo seed/reset | IMPLEMENTED | Admin-only controls and API create deterministic linked demo records and reset only registered demo records. Repeat reset/seed produces the same record identifiers. Official weather, historical datasets, non-demo operations and their audit history are preserved. |
| Demo guide | IMPLEMENTED AS GUIDED NAVIGATION | HQ sequence navigates existing pages and does not fake status transitions. The deterministic seed is a separate persisted demo scenario. |
| Shared shell and role navigation | IMPLEMENTED | Common authenticated top bar with intentionally distinct HQ, station and technical navigation. |
| Search, errors, loading, empty states | IMPLEMENTED IN PRIMARY VIEWS | Search/provenance and key empty/error/loading states were inspected. Exhaustive fault injection for every route was not performed. |
| Responsive behavior | VERIFIED FOR COMMAND CENTER | No horizontal overflow at 768×1024, 1024×768, 1366×768, 1440×900 or 1920×1080. Primary role screens were browser-captured at 1440×900. This is not exhaustive device/browser coverage. |
| Accessibility | BASIC CHECKS PASS; FULL AUDIT PENDING | Browser locators use visible names and labels for primary controls; semantic headings/forms are present. Contrast scoring, screen-reader review and comprehensive keyboard-only audit were not performed. |
| Performance | NO BLOCKING LOAD ISSUE OBSERVED | Playwright suite completes against fresh local services; production bundle built. No real-user metrics, throttled-network profile, or benchmark target was measured. Hero video remains metadata-preloaded with image fallback. |
| Routes | IMPLEMENTED VIA HASH NAVIGATION | HQ pane and station sync/state navigation exercised. API authorization remains enforced independently of the selected page. |
| Database integrity | PASS | Runtime SQLite read-only `integrity_check` returned `ok`; `foreign_key_check` returned no rows. Seed/reset preservation and determinism are covered with isolated test DB. |
| Actuator/autonomous control | NOT PRESENT IN CHECKED WORKFLOWS | No generator, CHP, pump, breaker, HVAC, battery, water or fuel actuation path was introduced. Product remains human-in-the-loop. |

## Demo data contents

`POST /api/demo/seed` creates the Maitri deterministic namespace `DEMO-PRATIBIMB-001`, including scenario/result, inspection operation and lifecycle events, manual station observations, report, sync records, reconciliation/review, evidence reference and a completed technical finding. IDs are stable (`SC-DEMO-001`, `OP-DEMO-001`, `OBS-DEMO-*`, `RPT-DEMO-001`, `SYNC-DEMO-001`, `REC-DEMO-001`, `EV-DEMO-001`, `TF-DEMO-001`).

Seed records use demo/manual provenance. The seed does not create or relabel an NCPOR observation. `POST /api/demo/reset` removes only records in this namespace and preserves non-demo operations/audit, NCPOR/cache/history, sessions and station configuration. Endpoint access is restricted to the Maitri HQ demo administrator.

The automated combined workflow also creates and processes fresh persisted records through the normal UI. It runs CHP Unavailable, queues a station inspection, records a manual station response offline, synchronizes, creates and completes a separate technical finding, performs an HQ Projection Check/review, and closes the station operation.

## Verification results

| Check | Result |
|---|---|
| `npm run build` | PASS — TypeScript and Vite production build |
| `python -m py_compile backend/main.py` | PASS |
| `python -m pytest backend/tests -q` | PASS — 18 tests |
| `npm run test:e2e` | PASS — 9 Playwright tests on the final code state |
| SQLite `PRAGMA integrity_check` | PASS — `ok` |
| SQLite `PRAGMA foreign_key_check` | PASS — no rows |
| Browser widths | PASS — Command Center no horizontal overflow at requested desktop widths and 768×1024 tablet-sized viewport |
| Role screens | PASS — landing, HQ Command Center, Digital Twin, Scenario Lab, Remote Operations, Reconciliation, station inbox/state/sync, technical overview/work captured at 1440×900 |
| Combined role journey | PASS — persisted HQ → station offline/sync → technical finding → HQ review/close flow in browser automation |

Playwright run contains nine scenarios: all-role sign-in/nav; HQ CHP scenario/request; station receive/work/offline/report/sync/reconciliation/review/close; technical observation/finding disposition; demo seed/reset; role/station authorization; combined three-role demonstration; 1440×900 primary-screen captures; Command Center responsive width checks.

## Remaining issues by severity

| Severity | Remaining item | Effect |
|---|---|---|
| MEDIUM | Offline mode is a client-side demonstration queue, not durable edge storage or a true disconnected operating client. | Do not represent the browser toggle as Antarctic network simulation or guarantee persistence after browser storage loss. |
| MEDIUM | Timed human-operated 3–5 minute presentation rehearsal was not performed. | The browser automation proves the flow, but presenter timing and handoff friction still need rehearsal. |
| MEDIUM | Live NCPOR provider reachability was not checked during this pass. | The UI handles unavailable observations and API tests validate metadata/cache behavior; current upstream availability is not certified. |
| LOW | Full accessibility audit and production performance/security assessment remain outstanding. | Contrast/screen-reader coverage and production-grade latency/credential/session controls were outside this prototype validation. |
| LOW | Audit integrity is enforced through application read-only surfaces and append-oriented events, not database-level tamper-proof triggers. | A local database administrator could alter persisted history outside normal workflows. |
| LOW | Authentication uses hard-coded demo credentials (`123`). | Keep this build local/prototype-only; replace authentication before deployment. |

No critical/high findings were identified by the checks in scope. This report does not certify production readiness or live Antarctic connectivity.
