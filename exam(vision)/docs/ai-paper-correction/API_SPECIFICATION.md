# EXAM Vision — API specification

Base URL `http://localhost:8006`. Interactive docs: `/api/docs`. All endpoints except
`/api/auth/status`, `/api/auth/setup`, `/api/auth/login` and `/api/health` need
`Authorization: Bearer <session token>`.

Roles: **admin**, **coordinator** (= manager), **teacher** (assigned exams only), **student** (own published results only).

## Auth, users, students
| Method | Path | Who | Purpose |
|---|---|---|---|
| GET | `/api/auth/status` | anyone | `{needs_setup}` |
| POST | `/api/auth/setup` | anyone, once | create the first admin |
| POST | `/api/auth/login` · `/logout` | — | session token |
| GET | `/api/auth/me` | signed in | current user |
| POST | `/api/auth/password` | signed in | change own password |
| GET/POST | `/api/users` | manager / admin | list / create (student accounts link a student) |
| PATCH | `/api/users/{id}` | admin | role, active, name, password reset |
| GET/POST | `/api/students` | staff / manager | list (filter `class_name`) / create |
| POST | `/api/students/import` | manager | CSV text: `admission_no,name,class_name,section,roll_no` |

## AI configuration
| Method | Path | Who | Purpose |
|---|---|---|---|
| GET | `/api/settings/ai` | admin | config (secrets masked) |
| PUT | `/api/settings/ai` | admin | `{changes:{...}}`: `claude_enabled`, `claude_api_key` (value sets, `""` clears, omitted keeps), `claude_vision_model`, `claude_eval_model`, `claude_effort`, `claude_timeout`, `max_retries`, `external_processing_acknowledged`, `redact_top_percent` |
| GET | `/api/settings/ai/status` | staff | whether reading and marking are available, and the models (no key) |
| POST | `/api/settings/ai/test/{reading\|marking}?sample=true` | admin | model check; `sample=true` reads a generated page |

## Exams, questions, rubrics
| Method | Path | Who | Purpose |
|---|---|---|---|
| GET/POST | `/api/exams` | staff / manager | list (teachers: assigned) / create |
| GET/PUT | `/api/exams/{id}` | staff / manager | detail with questions & current rubrics / update |
| PUT | `/api/exams/{id}/teachers` | manager | `{user_ids}` |
| POST | `/api/exams/{id}/questions` | manager | add question (`number`, `text`, `qtype`, `max_marks`, `mark_increment`, optional `numeric_answer`, `numeric_tolerance`, `unit`) |
| PUT/DELETE | `/api/questions/{id}` | manager | update (max locked once approved) / delete (only without answers) |
| POST | `/api/questions/{id}/rubric` | staff | save a **new rubric version** (criteria must add up to the question's marks; `source`: teacher / ai_draft) |
| POST | `/api/questions/{id}/rubric/draft` | staff | AI-drafted rubric for the form (not saved) |
| POST | `/api/exams/{id}/rubrics/draft-all` | staff | draft and save AI rubrics for questions without one |
| GET | `/api/questions/{id}/rubrics` | staff | version history |
| POST | `/api/exams/{id}/reevaluate` | staff | explicit re-evaluation of **unapproved** answers (`question_id` optional) |
| POST | `/api/exams/{id}/results/{reviewed\|approved\|published\|draft}` | manager | results workflow; approve/publish need every sheet approved |

## Answer sheets
| Method | Path | Who | Purpose |
|---|---|---|---|
| POST | `/api/exams/{id}/submissions` | staff | multipart: `student_id`, `files[]` (PDF or page photos, in order), `auto_process`; always stores the sheet, `note` says why reading did not start |
| POST | `/api/exams/{id}/submissions/bulk` | staff | multipart `files[]` named with admission numbers |
| GET | `/api/exams/{id}/submissions` | staff | list with totals, name check, Claude cost and last job |
| GET | `/api/submissions/{id}` | staff | everything for the review screen (incl. `name_check`, `cost`, `unassigned_text`) |
| DELETE | `/api/submissions/{id}` | manager | only when nothing is approved |
| GET | `/api/pages/{id}/image` | staff | page PNG |
| POST | `/api/pages/{id}/rotate` | staff | `{degrees: 90}` |
| POST | `/api/submissions/{id}/pages/order` | staff | `{page_ids}` |
| POST | `/api/submissions/{id}/identity` | staff | `{verified, student_id?}` |
| POST | `/api/submissions/{id}/process` | staff | Claude reads the whole sheet, then marks it |
| POST | `/api/submissions/{id}/extract` · `/evaluate` | staff | read only / mark only (`force`, `answer_ids`) |
| POST | `/api/submissions/{id}/extractions/{eid}/activate` | staff | use that reading |
| GET | `/api/jobs/{id}` | staff | job status |

## Answers
| Method | Path | Purpose |
|---|---|---|
| PATCH | `/api/answers/{id}/text` | teacher's corrected transcription (original kept) |
| POST | `/api/answers/{id}/verify` | transcription confirmed as read |
| POST | `/api/answers/{id}/evaluate` | re-evaluate this answer |
| PUT | `/api/answers/{id}/marks` | `{criterion_marks, reason, feedback}`; changing existing marks needs a reason and is recorded |
| POST | `/api/answers/{id}/approve` | needs marks, verified transcription and confirmed identity |
| POST | `/api/answers/{id}/reopen` · `/request-review` | `{reason}` |
| GET | `/api/answers/{id}/history` | evaluations and overrides |

## Reports
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/exams/{id}/report` | summary, students, question-wise, misconceptions, AI vs teacher, processing |
| GET | `/api/exams/{id}/report.csv` | student totals (UTF-8 with BOM for Excel) |
| GET | `/api/audit?entity_id=` | manager: audit trail |
| GET | `/api/my/results` | student: own **published** results |

## Evaluation output (validated)
```json
{"question_id": "1", "maximum_marks": 2, "suggested_marks": 2,
 "criteria": [{"criterion_id": "C1", "criterion_name": "...", "maximum_marks": 1, "awarded_marks": 1,
               "evidence": "verbatim quote", "explanation": "..."}],
 "strengths": [], "missing_concepts": [], "misconceptions": [], "student_final_answer": null,
 "ocr_status": "verified", "evaluation_confidence": null,
 "requires_teacher_review": false, "review_reasons": []}
```
`evaluation_confidence` is always `null`: model-reported confidence is not calibrated.
