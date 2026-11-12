"""Serverless-воркер H3: готовый ComfyUI-граф → видео + телеметрия.

Контракт `h3-serverless/v1.1`: вход `{input: {prompt, parameter, acceleration,
assets?, action?}}`, выход `{video | video_url, telemetry}`; источник истины —
`contract/worker-contract.md`.

Пакет намеренно не импортирует тяжёлые зависимости (`torch`, `boto3`,
`runpod`) на уровне модулей: тесты должны идти без GPU и сети.
"""

__version__ = "1.0.2"
