"""Load the demo exam, its rubrics and students into the EXAM database.

    python -m tools.seed_demo

Idempotent: an existing demo exam or student is left as it is. No user accounts are made:
the administrator is created on first sign-in.
"""

from __future__ import annotations

from sqlalchemy import select

from app import db
from app.models import Exam, Question, RubricVersion, Student

from .demo_data import EXAM, QUESTIONS, STUDENTS


def seed() -> str:
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
        s.commit()
        return exam.id


def main() -> None:
    exam_id = seed()
    print(f"demo exam ready ({exam_id}); students: {', '.join(s['admission_no'] for s in STUDENTS)}")


if __name__ == "__main__":
    main()
