"""The demo examination: questions, rubrics, students, their answers and a teacher's expected marks.

Used by make_dummy_sheets.py (renders the sheets), seed_demo.py (loads the exam) and
benchmark_providers.py (measures reading and marking against this ground truth).
"""

EXAM = {
    "name": "Unit Test 1 — Science (demo)",
    "academic_year": "2026-27",
    "class_name": "7",
    "section": "A",
    "subject": "Science",
    "grade_level": "CBSE Class 7",
    "instructions": "Answer in full sentences. Show working for calculations.",
}

QUESTIONS = [
    {
        "number": "1", "qtype": "science", "max_marks": 2, "mark_increment": 0.5,
        "text": "Why do plants need sunlight?",
        "rubric": {
            "criteria": [
                {"id": "C1", "name": "Identifies sunlight as the energy source for photosynthesis", "max_marks": 1},
                {"id": "C2", "name": "Explains that photosynthesis lets plants make their own food", "max_marks": 1},
            ],
            "reference_answer": "Plants use the energy of sunlight for photosynthesis, the process by which they make their own food.",
            "expected_concepts": ["light energy", "photosynthesis", "plants make their own food"],
            "alternatives": ["Plants use light energy to make their own food",
                             "Leaves capture sunlight to prepare food (glucose)"],
            "misconceptions": ["plants get their food from the soil"],
            "partial_credit_rules": "Naming photosynthesis without linking it to food earns C1 only.",
            "ignore_criteria": "Spelling and grammar are not assessed.",
        },
    },
    {
        "number": "2", "qtype": "science", "max_marks": 3, "mark_increment": 0.5,
        "text": "Explain the difference between evaporation and boiling.",
        "rubric": {
            "criteria": [
                {"id": "C1", "name": "Evaporation takes place only at the surface of the liquid", "max_marks": 1},
                {"id": "C2", "name": "Boiling takes place throughout the liquid (bubbles form inside)", "max_marks": 1},
                {"id": "C3", "name": "Evaporation happens at any temperature; boiling at a fixed boiling point", "max_marks": 1},
            ],
            "reference_answer": "Evaporation happens only at the surface and at any temperature; boiling happens throughout the liquid at a fixed temperature, the boiling point.",
            "misconceptions": ["boiling happens only at the surface", "evaporation needs heating"],
            "ignore_criteria": "Spelling and grammar are not assessed.",
        },
    },
    {
        "number": "3", "qtype": "math", "max_marks": 3, "mark_increment": 0.5,
        "text": "A car travels 120 km in 2 hours. Find its speed. Show your working.",
        "numeric_answer": "60", "unit": "km/h",
        "rubric": {
            "criteria": [
                {"id": "C1", "name": "Uses the formula speed = distance / time", "max_marks": 1},
                {"id": "C2", "name": "Substitutes 120 km and 2 h correctly", "max_marks": 1},
                {"id": "C3", "name": "Final answer 60 km/h with the unit", "max_marks": 1},
            ],
            "reference_answer": "Speed = distance / time = 120 km / 2 h = 60 km/h",
            "partial_credit_rules": ("Method marks (C1, C2) are given even if the arithmetic is wrong. "
                                     "Half a mark for C3 when the number 60 is right but the unit is missing."),
            "required_elements": "formula, working, unit",
        },
    },
    {
        "number": "4", "qtype": "short", "max_marks": 2, "mark_increment": 0.5,
        "text": "Write two functions of roots.",
        "rubric": {
            "criteria": [
                {"id": "C1", "name": "First valid function of roots", "max_marks": 1},
                {"id": "C2", "name": "Second, different valid function of roots", "max_marks": 1},
            ],
            "expected_concepts": ["absorb water", "absorb minerals", "anchor / hold the plant", "store food"],
            "alternatives": ["store food (e.g. carrot, radish)", "keep the plant standing"],
            "partial_credit_rules": "Water and minerals absorption together count as one function.",
        },
    },
]

# (admission_no, name, roll, font file, answers by question, teacher's expected marks)
STUDENTS = [
    {
        "admission_no": "A1001", "name": "Arun Kumar", "roll_no": "1", "font": "Inkfree.ttf", "pages": 2,
        "answers": {
            "1": "Plants use light energy to make their own food by photosynthesis.",
            "2": "Evaporation happens only at the surface of a liquid and at any temperature. "
                 "Boiling happens throughout the liquid at a fixed temperature called the boiling point.",
            "3": "Speed = Distance / Time\n= 120 km / 2 h\n= 60 km/h",
            "4": "1. Roots absorb water and minerals from the soil.\n2. Roots hold the plant firmly in the ground.",
        },
        "expected_marks": {"1": 2, "2": 3, "3": 3, "4": 2},
    },
    {
        "admission_no": "A1002", "name": "Divya S", "roll_no": "2", "font": "segoepr.ttf", "pages": 1,
        "answers": {
            "1": "Plants need sunlight for photosynthesis.",
            "2": "In boiling the bubbles form only at the surface. Evaporation is slow.",
            "3": "Speed = distance / time = 120 / 2 = 50 km/h",
            "4": "Roots absorb water.",
        },
        "expected_marks": {"1": 1, "2": 0, "3": 2, "4": 1},
    },
    {
        "admission_no": "A1003", "name": "Kiran R", "roll_no": "3", "font": "BRADHITC.TTF", "pages": 1,
        "answers": {
            "1": "The leaves catch the sun rays and with that energy they prepare food (glucose) for the plant.",
            "3": "120 / 2 = 60",
            "4": "They store food like in carrot. They also keep the tree standing in strong wind.",
        },
        "expected_marks": {"1": 2, "2": 0, "3": 1.5, "4": 2},
    },
]
