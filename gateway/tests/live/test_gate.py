"""The read-back gate on a spoken yes: the shape of an answer, and everything that is not one."""

from __future__ import annotations

from rc_gateway.live.gate import READBACK_SECONDS, ConfirmGate

DRAFT = "d1"


def ready() -> ConfirmGate:
    gate = ConfirmGate()
    gate.draft_ready(DRAFT)
    return gate


def answered(gate: ConfirmGate) -> None:
    gate.voice_out(READBACK_SECONDS)
    gate.person_onset(assistant_speaking=False)
    gate.person_transcript()


def test_a_draft_nobody_has_been_handed_cannot_be_confirmed() -> None:
    assert ConfirmGate().refusal(DRAFT) == "not_read_back"


def test_a_read_back_then_the_persons_voice_then_their_words_confirm() -> None:
    gate = ready()
    assert gate.refusal(DRAFT) == "not_read_back"
    gate.voice_out(READBACK_SECONDS)
    assert gate.refusal(DRAFT) == "not_confirmed"
    gate.person_onset(assistant_speaking=False)
    assert gate.refusal(DRAFT) == "not_confirmed"
    gate.person_transcript()
    assert gate.refusal(DRAFT) is None


def test_it_is_for_the_draft_that_was_handed_over_and_no_other() -> None:
    gate = ready()
    answered(gate)
    assert gate.refusal("another") == "not_read_back"


def test_words_without_a_voice_before_them_are_not_an_answer() -> None:
    gate = ready()
    gate.voice_out(READBACK_SECONDS)
    gate.person_transcript()  # a late fragment of the request, nobody spoke
    assert gate.refusal(DRAFT) == "not_confirmed"


def test_a_voice_before_the_read_back_does_not_count() -> None:
    gate = ready()
    gate.person_onset(assistant_speaking=False)
    gate.voice_out(READBACK_SECONDS)
    gate.person_transcript()
    assert gate.refusal(DRAFT) == "not_confirmed"


def test_the_assistant_speaking_before_the_draft_existed_arms_nothing() -> None:
    gate = ConfirmGate()
    gate.voice_out(READBACK_SECONDS)  # the delegation acknowledgement, however long
    gate.draft_ready(DRAFT)
    gate.person_onset(assistant_speaking=False)
    gate.person_transcript()
    assert gate.refusal(DRAFT) == "not_read_back"


def test_the_person_talking_over_the_assistant_spoils_the_draft_for_voice() -> None:
    gate = ready()
    gate.voice_out(READBACK_SECONDS)
    gate.person_onset(assistant_speaking=True)
    gate.person_transcript()
    assert gate.refusal(DRAFT) == "read_back_interrupted"
    # Read back in full again, and answered: the draft stays spoiled.
    answered(gate)
    assert gate.refusal(DRAFT) == "read_back_interrupted"


def test_an_interruption_after_the_answer_takes_the_answer_back() -> None:
    gate = ready()
    answered(gate)
    gate.person_onset(assistant_speaking=True)
    assert gate.refusal(DRAFT) == "read_back_interrupted"


def test_the_assistants_own_acknowledgement_after_the_answer_keeps_it() -> None:
    gate = ready()
    answered(gate)
    gate.voice_out(1.0)  # "Sending it now."
    assert gate.refusal(DRAFT) is None


def test_a_new_draft_needs_its_own_read_back_and_answer() -> None:
    gate = ready()
    answered(gate)
    gate.draft_ready("d2")
    assert gate.refusal("d2") == "not_read_back"
    assert gate.refusal(DRAFT) == "not_read_back"


def test_a_sent_or_cancelled_draft_confirms_nothing() -> None:
    gate = ready()
    answered(gate)
    gate.forget()
    assert gate.refusal(DRAFT) == "not_read_back"
    gate.person_onset(assistant_speaking=False)
    gate.person_transcript()
    assert gate.refusal(DRAFT) == "not_read_back"


def test_the_persons_voice_with_no_draft_pending_changes_nothing() -> None:
    gate = ConfirmGate()
    gate.person_onset(assistant_speaking=True)
    gate.person_onset(assistant_speaking=False)
    gate.person_transcript()
    gate.draft_ready(DRAFT)
    assert gate.refusal(DRAFT) == "not_read_back"


# ---- what makes a read-back ----


def test_two_seconds_of_voice_make_a_read_back_and_a_little_less_does_not() -> None:
    assert READBACK_SECONDS == 2.0
    short = ready()
    short.voice_out(READBACK_SECONDS - 0.05)
    assert short.refusal(DRAFT) == "not_read_back"
    enough = ready()
    enough.voice_out(READBACK_SECONDS)
    assert enough.refusal(DRAFT) == "not_confirmed"


def test_voice_adds_up_across_chunks_even_when_floats_sum_a_hair_short_of_two() -> None:
    gate = ready()
    total = 0.0
    for _ in range(10):
        total += 0.2
        gate.voice_out(0.2)
    # Added one by one, ten chunks of 0.2 s come to 1.9999999999999998: a hair short of two.
    assert total < READBACK_SECONDS
    assert gate.refusal(DRAFT) == "not_confirmed"


def test_a_short_read_back_that_was_refused_and_is_read_again_arms_it() -> None:
    gate = ready()
    gate.voice_out(1.2)
    assert gate.refusal(DRAFT) == "not_read_back"  # refused: the tally is not touched by that
    gate.voice_out(1.2)
    assert gate.refusal(DRAFT) == "not_confirmed"


def test_nothing_the_assistant_says_after_an_interjection_arms_the_spoiled_draft() -> None:
    gate = ready()
    gate.voice_out(1.5)  # the read-back, being said
    gate.person_onset(assistant_speaking=True)  # and the person cuts in
    gate.voice_out(1.2)  # "Let me check."
    gate.voice_out(1.2)  # the backend's spoken answer: more than a read-back's worth in all
    gate.person_onset(assistant_speaking=False)
    gate.person_transcript()  # and a "Thanks."
    assert gate.refusal(DRAFT) == "read_back_interrupted"


def test_an_interjection_empties_the_tally_and_the_voice_after_it_is_not_added() -> None:
    gate = ready()
    gate.voice_out(1.5)
    assert gate.voiced_seconds == 1.5
    gate.person_onset(assistant_speaking=True)
    assert gate.voiced_seconds == 0.0
    gate.voice_out(10.0)
    assert gate.voiced_seconds == 0.0


def test_a_new_draft_after_an_interjection_is_read_back_and_confirmed_afresh() -> None:
    gate = ready()
    gate.voice_out(1.5)
    gate.person_onset(assistant_speaking=True)
    gate.draft_ready("d2")  # the model prepared the message again
    assert gate.refusal("d2") == "not_read_back"
    gate.voice_out(READBACK_SECONDS - 0.1)
    assert gate.refusal("d2") == "not_read_back"
    gate.voice_out(0.1)
    assert gate.refusal("d2") == "not_confirmed"
    answered(gate)
    assert gate.refusal("d2") is None
    assert gate.refusal(DRAFT) == "not_read_back"  # the spoiled one is not the draft in question


def test_the_same_draft_handed_over_again_starts_afresh_too() -> None:
    gate = ready()
    gate.person_onset(assistant_speaking=True)
    gate.draft_ready(DRAFT)
    answered(gate)
    assert gate.refusal(DRAFT) is None


def test_a_sent_or_cancelled_draft_is_not_spoiled_any_more() -> None:
    gate = ready()
    gate.person_onset(assistant_speaking=True)
    gate.forget()
    assert gate.refusal(DRAFT) == "not_read_back"
    gate.draft_ready(DRAFT)
    assert gate.refusal(DRAFT) == "not_read_back"


def test_a_refusal_leaves_the_tally_alone() -> None:
    gate = ready()
    gate.voice_out(1.2)
    assert gate.refusal(DRAFT) == "not_read_back"
    assert gate.voiced_seconds == 1.2
    gate.person_onset(assistant_speaking=False)  # not an interjection: nobody was speaking
    gate.voice_out(1.2)
    assert gate.refusal(DRAFT) == "not_confirmed"


def test_handing_over_a_draft_empties_the_tally() -> None:
    gate = ready()
    gate.voice_out(1.5)
    gate.draft_ready("d2")
    gate.voice_out(1.5)
    assert gate.refusal("d2") == "not_read_back"
    gate.voice_out(0.5)
    assert gate.refusal("d2") == "not_confirmed"


def test_forgetting_a_draft_empties_the_tally() -> None:
    gate = ready()
    gate.voice_out(1.9)
    gate.forget()
    gate.draft_ready(DRAFT)
    gate.voice_out(0.2)
    assert gate.refusal(DRAFT) == "not_read_back"
