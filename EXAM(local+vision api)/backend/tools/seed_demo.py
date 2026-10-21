"""Load the demo exam, its rubrics and students into the EXAM database.

    python -m tools.seed_demo [--vision-key-file PATH]

Idempotent: an existing demo exam or student is left as it is. With --vision-key-file, the
file's sk_live_ key is stored (encrypted) as the Vision API key. No user accounts are made:
the administrator is created on first sign-in.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import select

from app import ai_config, db
from app.models import Exam, Question, RubricVersion, Student

from .demo_data import EXAM, QUESTIONS, STUDENTS


def seed(vision_key: str | None = None) -> str:
    db.configure()
    with db.new_session() as s:
        for st in STUDENTS:
            if not s.scalars(select(Student).where(Student.admission_no == st["admission_no"])).first():
                s.add(Student(admission_no=st["admission_no"], name=st["name"], class_name=EXAM["class_name"],
                              section=EXAM["section"], roll_no=st["roll_no"]))
        exam = s.scalars(select(Exam).where(Exam.name == EXAM["name"])).first()
        if exam is None:
            exam = Exam(**EXAM)
            s.add(exam)
            s.flush()
            for order, q in enumerate(QUESTIONS, start=1):
                question = Question(exam_id=exam.id, order=order, current_rubric_version=1,
                                    **{k: v for k, v in q.items() if k != "rubric"})
                s.add(question)
                s.flush()
                r = q["rubric"]
                s.add(RubricVersion(question_id=question.id, version=1, criteria=r["criteria"],
                                    reference_answer=r.get("reference_answer", ""),
                                    expected_concepts=r.get("expected_concepts", []),
                                    alternatives=r.get("alternatives", []), misconceptions=r.get("misconceptions", []),
                                    partial_credit_rules=r.get("partial_credit_rules", ""),
                                    required_elements=r.get("required_elements", ""),
                                    ignore_criteria=r.get("ignore_criteria", ""), subject_rules=r.get("subject_rules", "")))
        if vision_key:
            ai_config.save(s, {"own_vision_key": vision_key}, None)
        s.commit()
        return exam.id


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vision-key-file", type=Path)
    args = parser.parse_args()
    key = None
    if args.vision_key_file:
        text = args.vision_key_file.read_text(encoding="utf-8")
        key = next((w for w in text.split() if w.startswith("sk_live_")), None)
        if not key:
            raise SystemExit("no sk_live_ key found in that file")
    exam_id = seed(key)
    print(f"demo exam ready ({exam_id}); students: {', '.join(s['admission_no'] for s in STUDENTS)}"
          + ("; Vision API key stored" if key else ""))


if __name__ == "__main__":
    main()
