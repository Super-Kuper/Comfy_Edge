"""Точка входа RunPod serverless.

RunPod запускает ``main()``; handler — `h3_worker.handler.handler`. Импорт
`runpod` обязателен только здесь, поэтому пакет и тесты живут без него
(используется в образе, где runpod==2.2.1 из pyproject).
"""

from __future__ import annotations

import runpod

from .handler import handler


def main() -> None:
    """Запустить serverless-цикл RunPod с нашим handler'ом."""

    runpod.serverless.start({"handler": handler})


if __name__ == "__main__":
    main()
