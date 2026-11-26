# Pack D / 2z: settlement-year and bonus/overtime sources (read-only map)
Source SHA 778196c. New tests only, no core change.

| Source | File / function | Pinned by |
|---|---|---|
| Settlement year | Caller-supplied int `source_selection['source_year']` into `app/leave_annual_settlement_source.py::review_annual_settlement`. Dispatch: opening baseline year < source_year uses `leave_year2_recorded_source.build_year2_recorded_source`, else `leave_completed_year_source.review_completed_year_source` (calendar policy only, review date >= Jan 1 of next year, baseline year == source_year). Split math: `leave_annual_settlement.py::annual_settlement_split`. | SettlementYearSource |
| Sick-leave year | NOT FOUND as a defined source. Sick days only appear as monthly code `S` count (`leave_month_earning.py`) and policy flags in `leave_policy_preview.py` (`annual_deduct_sick`, `holiday_credit_during_sick`, `sick_alert_thresholds`). No code reads thresholds or defines a sick-year window. | SickLeaveYearSource (pins flags and the absence of a consumer) |
| Old monthly overtime | `payroll_workflow.py::recompute_payroll_item`, stored `overtime_hours` x `payroll_math.overtime_hourly_rate`, only when approved and `overtime_daily_digest` is None. | OldMonthlyOvertimeSource |
| Performance bonus | Saved `PayrollItem.performance_bonus` (models.py). Used in OT rate, gross, `leave_cash_math.calculate_leave_cash`, `pay_basis_source`. No cap or score. | PerformanceBonusSource |

Note: a code comment in `leave_annual_settlement_source.py` says the owner's examples retain the full entitlement / 2 basis. That is a claim inside the repository, not verified owner evidence. Tests pin the code behaviour only.

## Owner questions
1. SETTLED (owner, relayed 10:36): the sick-leave year is the calendar year, 1 January to 31 December. The 2ز helper is not wired yet.
2. What should `sick_alert_thresholds` (90, 180) do? Stored but unused.
3. Is "old monthly overtime" meant to stay once daily overtime is required for a run, or be retired?
4. Is a performance bonus cap or score intended? None exists.
