"""Check the exact model connections the two pipeline agents use."""
import json
import os
import re
import subprocess


def check_pmi_model(model):
    from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

    base = os.getenv("PMI_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    key = os.getenv("PMI_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not model or not base or not key:
        raise RuntimeError("Укажите модель, адрес сервера и API-ключ для субагента ПМИ.")
    try:
        client = OpenAI(base_url=base, api_key=key, timeout=30, max_retries=0)
        response = client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": "Reply with exactly OK."}],
            max_tokens=128,
        )
    except APITimeoutError as exc:
        raise RuntimeError("Модель не ответила за 30 с. Проверьте доступ к серверу и VPN.") from exc
    except APIConnectionError as exc:
        raise RuntimeError("Нет соединения с сервером модели. Проверьте адрес и VPN.") from exc
    except APIStatusError as exc:
        raise RuntimeError(f"Сервер модели вернул HTTP {exc.status_code}.") from exc
    if not response.choices or not (response.choices[0].message.content or "").strip():
        raise RuntimeError("Сервер ответил, но вернул пустой ответ модели.")


def check_pi_model(command, root):
    probe = [
        *command, "--no-tools", "--no-session", "--no-extensions", "--no-mcp",
        "--no-skills", "--no-context-files", "--mode", "json", "--print",
        "Reply in one short sentence.",
    ]
    try:
        result = subprocess.run(probe, cwd=root, capture_output=True, text=True, timeout=45)
    except FileNotFoundError as exc:
        raise RuntimeError("Pi CLI не найден. Установите зависимости проекта.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Pi не получил ответ модели за 45 с. Проверьте сервер и VPN.") from exc
    assistant_errors = []
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "message_end":
            continue
        message = event.get("message") or {}
        if message.get("role") != "assistant":
            continue
        has_text = any(
            block.get("type") == "text" and str(block.get("text") or "").strip()
            for block in message.get("content", [])
        )
        if result.returncode == 0 and message.get("stopReason") == "stop" and has_text:
            return
        if message.get("errorMessage"):
            assistant_errors.append(str(message["errorMessage"]))
    details = "\n".join([result.stderr, *assistant_errors]).lower()
    if "no models" in details or "model not found" in details or "unknown model" in details:
        raise RuntimeError("Pi не нашёл модель в провайдере. Проверьте настройки провайдера Pi.")
    if "api key" in details or re.search(r"\b(401|403)\b", details):
        raise RuntimeError("Pi не получил доступ к модели. Проверьте API-ключ провайдера.")
    if "timed out" in details or "timeout" in details:
        raise RuntimeError("Pi сообщил о таймауте запроса к модели.")
    if not result.returncode and not assistant_errors:
        raise RuntimeError("Pi завершился, но модель не вернула законченный текстовый ответ.")
    raise RuntimeError(f"Pi не получил проверочный ответ модели (код {result.returncode}).")
