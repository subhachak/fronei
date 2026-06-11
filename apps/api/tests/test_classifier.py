from app.services.classifier import classify_task


def test_coding_keyword():
    task, _, _ = classify_task("fix the bug in this Python function")
    assert task == "coding"


def test_no_false_positive_api_in_capital():
    task, _, _ = classify_task("What is the capital of France?")
    assert task != "coding"


def test_no_false_positive_code_in_decode():
    _, _, reason = classify_task("How do I decode a base64 string?")
    assert "word-boundary" in reason


def test_architecture_keyword():
    task, _, _ = classify_task("Design a microservice architecture for payments")
    assert task == "architecture"


def test_complexity_high_by_length():
    _, complexity, _ = classify_task("x" * 2600)
    assert complexity == "high"


def test_complexity_medium_by_marker():
    _, complexity, _ = classify_task("Please analyze this code snippet")
    assert complexity == "medium"


def test_complexity_low_default():
    _, complexity, _ = classify_task("Hello")
    assert complexity == "low"


def test_unknown_task():
    task, _, _ = classify_task("Hello, how are you?")
    assert task == "unknown"
