from app.core.config import get_settings
from concurrent.futures import ThreadPoolExecutor, TimeoutError

try:
    import google.generativeai as genai
except Exception:  # pragma: no cover
    genai = None


settings = get_settings()


def _normalize_model_name(model_name: str) -> str:
    if not model_name:
        return model_name
    return model_name if model_name.startswith("models/") else model_name


def _extract_text(response) -> str:
    if response is None:
        return ""
    text = getattr(response, "text", None)
    if text:
        return text
    # Fallback for cases where .text is empty
    try:
        parts = response.candidates[0].content.parts
        if parts:
            return parts[0].text or ""
    except Exception:
        return ""
    return ""


def generate_text_response(prompt: str, temperature: float = 0.3, timeout_seconds: int = 45) -> str:
    if not settings.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    if genai is None:
        raise RuntimeError("google-generativeai is not installed")

    genai.configure(api_key=settings.GEMINI_API_KEY)
    model_name = _normalize_model_name(settings.GEMINI_MODEL)

    model = genai.GenerativeModel(
        model_name,
        generation_config={
            "temperature": temperature,
        }
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(model.generate_content, prompt)
        try:
            response = future.result(timeout=timeout_seconds)
        except TimeoutError as exc:
            raise RuntimeError("Model response timed out") from exc
    text = _extract_text(response)
    if not text:
        raise RuntimeError("Empty response from model")
    return text


def generate_recommendation(prompt: str) -> str:
    if not settings.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    if genai is None:
        raise RuntimeError("google-generativeai is not installed")

    genai.configure(api_key=settings.GEMINI_API_KEY)
    model_name = _normalize_model_name(settings.GEMINI_MODEL)

    model = genai.GenerativeModel(
        model_name,
        generation_config={
            "temperature": 0.4,
            "response_mime_type": "application/json"
        }
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(model.generate_content, prompt)
        try:
            response = future.result(timeout=60)
        except TimeoutError as exc:
            raise RuntimeError("Recommendation generation timed out") from exc
    text = _extract_text(response)
    if not text:
        raise RuntimeError("Empty response from recommendation model")
    return text


