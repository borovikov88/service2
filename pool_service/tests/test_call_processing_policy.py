"""Metadata-only policy tests: no network, audio, Django database or paid API."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from pool_service.services.call_processing_policy import (
    ALL_EXCEPT, ALLOWLIST, MANUAL, CallFacts, Rule, decide_call, parse_numbers, phone_key,
)


class CallProcessingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        self.call = CallFacts(
            participants=(1,), counterpart_numbers=frozenset({"+12025550101"}),
            started_at=self.now, verified=True, duration_seconds=120, audio_ready=True,
        )
        self.all = Rule(mode=ALL_EXCEPT, effective_from=self.now - timedelta(days=1))
        self.rules = {1: self.all, 2: self.all}

    def decision(self, call=None, *, rules=None, personal=None, simulation=False):
        return decide_call(
            call or self.call, self.rules if rules is None else rules,
            personal or {}, simulation=simulation,
        )

    def internal(self, **changes):
        call = replace(
            self.call,
            participants=(1, 2),
            direction="internal",
            counterpart_numbers=frozenset({"+12025550101", "+12025550102"}),
            counterpart_numbers_by_participant=(
                (1, frozenset({"+12025550102"})),
                (2, frozenset({"+12025550101"})),
            ),
        )
        return replace(call, **changes)

    def test_unknown_crm_client_is_allowed_on_employee_line(self):
        self.assertEqual(self.decision().action, "transcribe")

    def test_outbound_calls_use_the_same_rules(self):
        self.assertEqual(self.decision(replace(self.call, direction="out")).action, "transcribe")

    def test_new_employee_without_rule_is_manual(self):
        self.assertEqual(self.decision(rules={}).reason, "manual")

    def test_allowlist_matches_only_work_numbers(self):
        rule = Rule(mode=ALLOWLIST, work_numbers=self.call.counterpart_numbers,
                    effective_from=self.all.effective_from)
        self.assertEqual(self.decision(rules={1: rule}).action, "transcribe")
        self.assertEqual(self.decision(rules={1: replace(rule, work_numbers=frozenset())}).reason, "not_allowed")

    def test_owner_can_allow_internal_staff_without_external_numbers(self):
        internal = self.internal(counterpart_numbers=frozenset())
        owner = replace(self.all, mode=ALLOWLIST, include_staff=True)
        self.assertTrue(self.decision(internal, rules={1: owner, 2: self.all}).selected)
        self.assertFalse(self.decision(internal, rules={1: replace(owner, include_staff=False), 2: self.all}).selected)

    def test_personal_exclusion_wins_over_allowlist(self):
        owner = replace(self.all, mode=ALLOWLIST, work_numbers=self.call.counterpart_numbers)
        self.assertEqual(self.decision(rules={1: owner}, personal={1: self.call.counterpart_numbers}).reason, "personal")

    def test_personal_exclusion_is_scoped_to_participating_owner(self):
        self.assertEqual(self.decision(personal={3: self.call.counterpart_numbers}).action, "transcribe")

    def test_peer_owner_exclusion_cannot_be_bypassed_by_employee_rule(self):
        internal = self.internal(participants=(2, 1))
        self.assertEqual(
            self.decision(internal, personal={1: frozenset({"+12025550102"})}).reason,
            "personal",
        )

    def test_unverified_or_missing_internal_party_fails_closed(self):
        for call in (replace(self.call, verified=False), replace(self.call, participants=()),
                     replace(self.call, direction="internal"), replace(self.call, direction="internal", participants=(1, 1))):
            with self.subTest(call=call):
                self.assertEqual(self.decision(call).reason, "mapping_required")

    def test_manual_peer_is_not_overridden_by_the_other_participant(self):
        internal = self.internal()
        self.assertEqual(self.decision(internal, rules={1: self.all, 2: Rule(mode=MANUAL)}).reason, "manual")

    def test_new_rules_do_not_process_the_old_archive(self):
        old = replace(self.call, started_at=self.now - timedelta(days=10))
        self.assertEqual(self.decision(old).reason, "historical")
        self.assertTrue(self.decision(old, simulation=True).selected)

    def test_preview_never_bypasses_privacy_or_mapping(self):
        self.assertEqual(self.decision(personal={1: self.call.counterpart_numbers}, simulation=True).reason, "personal")
        self.assertEqual(self.decision(replace(self.call, verified=False), simulation=True).reason, "mapping_required")

    def test_unactivated_or_naive_rule_is_not_runtime_permission(self):
        for effective in (None, self.now.replace(tzinfo=None)):
            self.assertEqual(self.decision(rules={1: replace(self.all, effective_from=effective)}).reason, "activation_required")

    def test_waits_for_audio_not_just_call_metadata(self):
        result = self.decision(replace(self.call, audio_ready=False))
        self.assertEqual(result.action, "wait_audio")
        self.assertTrue(result.selected)

    def test_saved_transcript_is_reused_without_another_transcription(self):
        result = self.decision(replace(self.call, saved_transcript=True, audio_ready=False))
        self.assertEqual(result.action, "resume_analysis")

    def test_ready_or_processing_result_is_not_restarted(self):
        for status in ("ready", "processing"):
            result = self.decision(replace(self.call, analysis_status=status))
            self.assertFalse(result.selected)
            self.assertEqual(result.reason, "already_" + status)

    def test_private_ready_result_does_not_disclose_its_status(self):
        self.assertEqual(self.decision(replace(self.call, analysis_status="ready"), personal={1: self.call.counterpart_numbers}).reason, "personal")

    def test_missed_empty_uploaded_or_unknown_direction_are_skipped(self):
        for call in (replace(self.call, answered=False), replace(self.call, duration_seconds=0),
                     replace(self.call, source="uploaded"), replace(self.call, direction="unknown")):
            self.assertFalse(self.decision(call).selected)

    def test_unknown_phone_or_unresolved_private_internal_match_is_skipped(self):
        self.assertEqual(self.decision(replace(self.call, counterpart_numbers=frozenset())).reason, "number_unknown")
        internal = replace(
            self.call,
            direction="internal",
            participants=(1, 2),
            counterpart_numbers=frozenset(),
            counterpart_numbers_by_participant=(),
        )
        self.assertEqual(
            self.decision(internal, personal={1: self.call.counterpart_numbers}).reason,
            "privacy_mapping_required",
        )

    def test_internal_call_fails_closed_when_only_one_peer_is_privacy_checkable(self):
        internal = self.internal(
            counterpart_numbers_by_participant=(
                (1, frozenset()),
                (2, frozenset({"+12025550101"})),
            )
        )
        self.assertEqual(
            self.decision(internal, personal={2: frozenset()}).reason,
            "privacy_mapping_required",
        )

    def test_invalid_rule_fails_closed(self):
        self.assertEqual(self.decision(rules={1: Rule(mode="typo")}).reason, "invalid_rule")

    def test_number_list_normalizes_and_deduplicates_crm_phone_keys(self):
        values = parse_numbers("+7 (999) 000-00-01\n8 999 000 00 01;9990000001,+1 202 555 0101")
        self.assertEqual(values, ("+12025550101", "9990000001"))
        self.assertEqual(phone_key("+44 20 7946 0958"), "+442079460958")

    def test_invalid_numbers_extensions_and_number_overflow_are_rejected(self):
        for text in ("123", "mother 79990000001", "+1+2025550101", "7" * 100,
                     "\n".join("+1202" + str(5550000 + n) for n in range(501))):
            with self.subTest(length=len(text)), self.assertRaises(ValueError):
                parse_numbers(text)
        self.assertEqual(parse_numbers(" \n "), ())


if __name__ == "__main__":
    unittest.main()
