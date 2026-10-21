# EXAM Vision — Testing

## Automated (37 tests, all passing; Claude replaced by fakes, no cost)

`cd backend` → `.venv\Scripts\python.exe -m pytest tests -q`

* Reader parsing: all pages + question list sent in one streamed request; schema restricts
  question numbers; out-of-range pages dropped; a question answered twice keeps both parts;
  name and unassigned writing returned; acknowledgement required.
* Name check: tolerant of small spelling differences; "differs" flagged; never changes the student.
* Scoring: limits, steps, negatives, evidence, unknown/missing criteria, totals recomputed,
  numeric final answer, arithmetic slips, review reasons kept.
* Workflow: upload → read → mark → identity → override (reason) → approve → publish → student
  sees only published results; cost per sheet; reading failure keeps the sheet and can be retried;
  without acknowledgement sheets are stored but not read; uncertain words verified before marking;
  invalid AI output retried then left to the teacher; missing rubric flagged; rubric changes never
  touch approved marks; duplicates; bulk upload; blank answer = 0 without a call; settings
  validation; AI rubric drafting.
* Permissions and secrets: unassigned teachers get 404, students blocked, key never returned.

## Live benchmark (2026-10-14, Claude Opus 5.5, effort medium, 3 demo sheets)

| | Result |
|---|---|
| Reading — word error rate | **0.0** |
| Question mapping | **100 %** |
| Numbers | **100 %** |
| Names read | 3 / 3 matched |
| Time per sheet | 6.3 s (4.7–8.8 s) |
| Reading cost | $0.10 for 3 sheets |
| Marking vs teacher key | **11 / 12 exact, 12 / 12 within half a mark**, mean difference 0.04 |
| Marking cost | $0.17 for 12 answers |

The one difference (Kiran Q3, "120 / 2 = 60" without the formula written): Claude gave the
formula mark and flagged it — "C1 is ambiguous … the teacher should decide" — which is the
intended behaviour. UI walked through on a scratch copy with Divya's sheet: read in 4.7 s,
name matches, 4 / 10 (= teacher key), $0.09.

Demo sheets are font-rendered; benchmark on 20–30 real teacher-marked sheets before relying on
the marks, and keep teacher approval mandatory.
