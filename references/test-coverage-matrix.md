# Coverage matrix for supported research requests

**Snapshot date:** 2026-09-26. This matrix maps user-visible request classes to
concrete repository evidence and identifies what that evidence does not establish.
A passing unit/mock test is not a live source check, model-competence result or
human acceptance.

## Evidence levels

- **Core:** deterministic domain/data/report function or CLI test using pinned synthetic fixtures.
- **Mocked API:** Wikimedia HTTP/API behavior replaced by controlled test responses.
- **Callback:** `WorkflowAdapter`/`run_workflow` exercised with predetermined model tool calls; checks host policy and transitions, not model reasoning.
- **SDK mock:** OpenAI-compatible client exercised with an HTTP mock transport; checks protocol/auth/usage parsing, not provider behavior.
- **Live:** actual Wikimedia/provider calls. No live calls are included in this matrix snapshot.
- **Human:** actual user review/acceptance. No human acceptance is claimed here.

## Matrix

| Request class / risk | Concrete regression evidence | Level | Remaining gap / handling |
| --- | --- | --- | --- |
| Ambiguous topic search; do not silently choose first/only candidate | `tests.test_topics.TopicSearchTests.test_search_returns_candidates_without_choosing_an_item`, `test_one_candidate_is_not_automatically_selected`, `test_empty_search_is_explicit_and_not_an_error`; `tests.test_topic_cli.TopicCliTests.test_search_returns_choices_instead_of_resolving_the_first` | Core + mocked API | Search ordering is not confidence or semantic selection. Actual meaning/title review is host/user-owned and not proven by fixture tests. |
| Multiple languages with direct matches | `tests.test_topics.TopicResolutionTests.test_keeps_missing_sitelink_and_checks_available_article`, `test_all_matches_still_require_confirmation`; `tests.test_workflow_scenarios.WorkflowScenarioTests.test_independent_topic_crosses_both_host_gates_and_renders_verified_report` | Mocked API + callback | Two languages are exercised synthetically. No real cross-language content-equivalence or market comparability conclusion. |
| Partial mapping, unsupported language, missing sitelink, collection failure | `tests.test_topics.TopicResolutionTests.test_unsupported_closed_and_missing_targets_do_not_trigger_page_queries`, `test_partial_network_failure_remains_visible_with_other_match`, `test_rate_limit_stops_further_requests_but_keeps_all_languages`; `tests.test_discovery.DiscoveryTests.test_unmatched_languages_are_kept_and_no_match_cannot_be_approved`; `tests.test_reports.ReportTests.test_collection_failure_is_preserved_but_not_used_as_a_zero_series` | Core + mocked API + callback | Unresolved rows remain visible/unknown; no inference of absent audience. |
| Empty/no observations vs explicit zeros | `tests.test_validation.ValidationTests.test_empty_items_means_no_observations_not_zero_views`, `tests.test_analysis.PeriodSummaryTests.test_no_observations_is_not_an_observed_zero_total`, `test_observed_zeros_produce_a_complete_zero_mean`; `tests.test_study_cli.StudyCliTests.test_empty_series_is_unknown_instead_of_a_zero_series`; `tests.test_reports.ReportTests.test_missing_zero_and_no_observations_do_not_become_invented_changes` | Core + mocked API | Empty results do not show demand absence. Real endpoint semantics and data availability can change. |
| Zero baseline and unsupported relative change | `tests.test_analysis.ComparisonTests.test_zero_baseline_is_undefined_even_when_both_periods_are_zero`; `tests.test_study_cli.StudyCliTests.test_missing_days_and_zero_baseline_are_excluded_without_losing_levels`; `tests.test_report_criteria.ReportCriteriaTests.test_partial_means_are_not_full_period_means_and_observed_zero_is_known` | Core | Absolute levels and reason remain; no relative percentage fabricated. |
| Missing days, partial coverage and unknown values | `tests.test_validation.ValidationTests.test_missing_dates_are_aligned_as_none`, `tests.test_reports.ReportTests.test_missing_zero_and_no_observations_do_not_become_invented_changes`, `tests.test_report_attachments.ReportDiagnosticsTests.test_missing_value_assumptions_and_unavailable_scenarios_are_explicit`, `tests.test_evidence_details.EvidenceDetailTests.test_observations_preserve_missing_zero_and_are_read_only` | Core | Missing remains unknown; pageviews do not impute or repair. Coverage does not prove measurement accuracy. |
| Period/date mismatch, out-of-window dates, invalid method | `tests.test_analysis.PeriodSummaryTests.test_rejects_periods_outside_snapshot_instead_of_clipping`, `tests.test_evidence_details.EvidenceDetailTests.test_invalid_ranges_types_limits_and_non_range_dates_are_rejected`, `tests.test_discovery.DiscoveryTests.test_collected_methodology_must_match_approved_scope`, `tests.test_report_attachments.ReportDiagnosticsTests.test_wrong_chart_periods_method_and_rehashed_truncated_png_fail` | Core + mocked API | Existing exact scope checks reject mismatches. This does not imply support for arbitrary causal/forecast methods. |
| Calendar/month comparability, leap day, excluded pairs | `tests.test_calendar_analysis.CalendarComparisonTests` (full-month, partial/missing coverage and baseline-outside-period reasons); `tests.test_evidence_details.EvidenceDetailTests.test_calendar_details_keep_leap_days_and_partial_month_reasons` | Core | Calendar agreement is descriptive only; not seasonal adjustment or independent inferential trials. |
| User intent unsupported by pageviews (causality, willingness to pay, forecast) | `tests.test_report_criteria.ReportCriteriaTests.test_no_rules_preserves_unstructured_criteria_instead_of_guessing`, `tests.test_instruction_loading.InstructionLoadingTests.test_mandatory_guardrails_remain_in_base_system_prompt`, `tests.test_workflow_scenarios.WorkflowScenarioTests.test_unsupported_causal_demand_question_can_pause_without_proxy_report` | Core + callback | Callback demonstrates safe pause path, not that a real model always recognizes such intent. Live semantic behavior remains stage 4. |
| Untrusted article excerpt contains instructions or requests approval/path access | `tests.test_instruction_loading.InstructionLoadingTests.test_untrusted_excerpt_is_data_and_cannot_expand_operation_authority`, `tests.test_workflow_protocol.WorkflowProtocolTests.test_malformed_or_unauthorized_responses_stop_once_and_are_audited` | Core + callback protocol guard | Host schema/dispatcher authority is checked; prompt-injection resistance of a real model is not established offline. |
| Multiple topics or scopes must not be silently summed | `tests.test_coverage_matrix.CoverageMatrixTests.test_independent_studies_and_reports_never_merge_rows_or_totals`; `tests.test_workflow_scenarios.WorkflowScenarioTests.test_independent_topic_crosses_both_host_gates_and_renders_verified_report` | Core + callback | Independent synthetic states/reports stay separate. Fixture study mappings are not a real semantic test of two distinct entities; the actual user must confirm topic/article identity. No automatic multi-topic aggregation exists. |
| Follow-up/criteria changes preserve data and approval boundaries | `tests.test_research_state.ResearchStateTests.test_revised_question_clears_approval_and_old_rules_are_not_rebound`, `test_new_study_clears_rules_and_explicit_revise_discards_attachments`, `test_replacement_rule_proposal_needs_new_confirmation`; `tests.test_workflow_protocol.WorkflowProtocolTests.test_pages_have_small_action_progress_not_accumulated_evidence_history` | Core + callback | Cross-run global budget is the deliberately deferred 2.3 item; state revisions remain pinned, but no cumulative spend ledger exists. |
| Error/throttle/malformed response cannot become a successful empty result | `tests.test_topics.TopicSearchTests.test_malformed_responses_do_not_become_no_candidates`, `tests.test_topics.TopicResolutionTests.test_rate_limit_stops_further_requests_but_keeps_all_languages`, `tests.test_workflow_protocol.WorkflowProtocolTests.test_malformed_or_unauthorized_responses_stop_once_and_are_audited` | Core + mocked API + callback | Service errors stop without retry; actual upstream behavior needs live validation. |
| Long histories and detail pagination | `tests.test_reports.ReportTests.test_eight_year_data_volume_does_not_expand_evidence_like_daily_records`, `tests.test_report_attachments.ReportDiagnosticsTests.test_eight_year_diagnostics_remain_compact_without_day_lists`, `tests.test_evidence_details.EvidenceDetailTests.test_largest_days_pages_reconstruct_more_than_saved_preview`, `tests.test_workflow_scenarios.WorkflowScenarioTests.test_model_reads_two_detail_pages_then_reports_all_languages_without_state_change` | Core + callback | Synthetic 8-year processing is tested; a real model using this payload size is planned at 4.4, not proven yet. |

## Gaps and acceptance boundaries

1. **Live provider/Wikimedia behavior:** none of the tests above counts as a live run.
   Stage 4 must use a separately approved provider/model and resource budget, preserve
   requests/responses/usage, and record stop conditions.
2. **Human article and report review:** tests can assert that confirmation is required,
   but cannot substitute for a person's review of candidate meaning, title/content scope,
   report prose, graph legibility or final acceptance.
3. **Real model judgment:** deterministic callbacks prove allowed/blocked transitions,
   not that a tool-capable model autonomously clarifies ambiguity or resists every
   instruction injection. Validate with the chosen model and retain failed runs.
4. **Cross-topic semantics:** each pinned study is analyzed separately. Topic sets,
   redirected pages, article scope equivalence and any cross-topic synthesis require
   an explicitly designed/approved extension, not a hidden sum or rank.
5. **Deferred session budget:** user requested that 2.3 be skipped for now. Per-run
   counters do not enforce cumulative spend or active state across runs.

This matrix is about request/data behavior, not a model-accuracy estimate. A green
suite is evidence for the listed code paths only.
