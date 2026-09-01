from app.services.celpip.lessons_content import LESSONS, TASK_PLAYBOOKS
from app.services.celpip.spec import TASKS_BY_KEY


def test_every_official_task_has_a_detailed_learning_playbook() -> None:
    task_lessons = {lesson.get("task_key"): lesson for lesson in LESSONS if lesson.get("task_key")}

    assert set(task_lessons) == set(TASKS_BY_KEY)
    assert set(TASK_PLAYBOOKS) == set(TASKS_BY_KEY)
    for task_key, lesson in task_lessons.items():
        body = lesson["body"]
        assert "## Detailed execution plan" in body, task_key
        assert "## Fast decision rules" in body, task_key
        assert "## Traps to recognize immediately" in body, task_key
        assert "## A focused 10-minute drill" in body, task_key
        assert lesson["estimated_minutes"] >= 9, task_key
