"""The connected model: config, one call, and the audit log. Nothing else
in the tool talks to the network."""

import json
import os
import time
from pathlib import Path

from .hub import home_dir

LLM_DEFAULTS = {
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-flash",
    "timeout": 90,
}


def llm_config() -> dict | None:
    """Config de la LLM conectada (OpenAI-compatible). None si no hay clave."""
    base = str(os.environ.get("SKILLGRAM_LLM_BASE_URL") or LLM_DEFAULTS["base_url"])
    model = str(os.environ.get("SKILLGRAM_LLM_MODEL") or LLM_DEFAULTS["model"])
    key = os.environ.get("SKILLGRAM_LLM_API_KEY")
    if not key:
        for env_name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
            key = os.environ.get(env_name)
            if key:
                break
    if not key:
        return None
    if base.endswith("/v1") or "/v" in base.rsplit("/", 1)[-1]:
        url = base.rstrip("/") + "/chat/completions"
    else:
        url = base.rstrip("/") + "/v1/chat/completions"
    return {"url": url, "model": model, "key": key, "timeout": LLM_DEFAULTS["timeout"]}


def llm_chat(cfg: dict, system: str, user: str) -> str:
    """Un turno contra la LLM conectada. Devuelve el texto o levanta OSError."""
    import urllib.error
    import urllib.request

    payload = json.dumps(
        {
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": 4000,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        cfg["url"],
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg['key']}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise OSError(f"HTTP {exc.code}: {exc.read()[:300].decode('utf-8', 'replace')}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OSError(f"LLM inalcanzable: {exc}") from exc
    try:
        return body["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise OSError(f"respuesta LLM inesperada: {str(body)[:300]}") from exc


LLM_SYSTEM = (
    "You are skillgram's consolidation assistant. You receive a JSON list of skills "
    "found in several agent harnesses. Reply ONLY with a JSON object: "
    '{"skills":[{"name":"...","harness":"...","category":"...","flavor":"generic|claude|codex|hermes|openai|other",'
    '"duplicate_of":"name-or-empty","reason":"short"}]}. '
    "Rules: use one of the given hub categories when possible; flavor=generic unless the "
    "skill text hard-codes one agent's CLI or tooling; duplicate_of only when the two "
    "skills are the same tool with different wording; never invent skills."
)


def llm_log_path() -> Path:
    """The audit log of every LLM call: who asked, what for, which model."""
    return home_dir() / ".agents" / "skillgram-llm.log"


def llm_log(action: str, detail: dict, model: str = "") -> None:
    try:
        llm_log_path().parent.mkdir(parents=True, exist_ok=True)
        with llm_log_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "action": action,
                                 "model": model, "detail": detail}, ensure_ascii=False) + "\n")
    except OSError:
        pass
