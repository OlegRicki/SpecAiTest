"""Судья LLM для ответов чата. Без ключа/сети — skip + метка в Allure."""

from __future__ import annotations

import json
import os
from typing import Sequence

import allure
import pytest
import requests


def llm_unavailable_reason() -> str | None:
    base = os.environ.get("OPENAI_BASE_URL", "").strip()
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()
    missing = [
        name
        for name, value in (
            ("OPENAI_BASE_URL", base),
            ("OPENAI_API_KEY", key),
            ("LLM_MODEL", model),
        )
        if not value
    ]
    if missing:
        return "Нет в .env: " + ", ".join(missing)
    return None


def _mark_unavailable(reason: str) -> None:
    allure.dynamic.tag("llm-unavailable")
    allure.dynamic.label("llm", "unavailable")
    allure.attach(reason, "LLM недоступен", allure.attachment_type.TEXT)
    pytest.skip(f"LLM недоступен: {reason}")


def assert_chat_quality(
    question: str,
    answer: str,
    criteria: Sequence[str],
) -> None:
    """
    Проверяет текст ответа чата критериями из блока «Проверка LLM:».
    Если LLM недоступна — тест skipped, в Allure тег llm-unavailable.
    """
    reason = llm_unavailable_reason()
    if reason:
        _mark_unavailable(reason)

    criteria_text = "\n".join(f"- {item}" for item in criteria) or "- ответ по теме вопроса"
    prompt = (
        "Ты судья автотеста чата. Ответь ТОЛЬКО JSON вида "
        '{"pass": true/false, "reason": "кратко почему"}.\n'
        f"Вопрос пользователя:\n{question}\n\n"
        f"Ответ чата:\n{answer}\n\n"
        f"Критерии:\n{criteria_text}\n"
    )
    base = os.environ["OPENAI_BASE_URL"].rstrip("/")
    url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
    headers = {
        "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": os.environ["LLM_MODEL"],
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        _mark_unavailable(str(exc))
        return

    raw = (content or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.removeprefix("json").strip()
    try:
        verdict = json.loads(raw)
        passed = bool(verdict.get("pass"))
        why = str(verdict.get("reason") or "")
    except json.JSONDecodeError:
        passed = False
        why = f"Судья вернул не JSON: {raw[:400]}"

    allure.attach(question, "Вопрос чату", allure.attachment_type.TEXT)
    allure.attach(answer, "Ответ чата", allure.attachment_type.TEXT)
    allure.attach(why or json.dumps(verdict, ensure_ascii=False), "Вердикт LLM", allure.attachment_type.TEXT)
    assert passed, why or "Ответ чата не прошёл проверку LLM"
