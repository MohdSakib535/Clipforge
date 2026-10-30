"""Reel scripts from a local LLM (Ollama, ``REEL_SCRIPT_MODEL``).

One call per script (~15 s with gemma3:4b on an M4): the reply must follow a
JSON schema, is written in the chosen language and sized to the reel's length.
The model is unloaded right after (keep_alive 0) so the image model gets the
RAM. Small models don't always follow the rules (they once put the whole
script into scene 1), so the reply is cleaned up in ``normalize_script``.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass

import httpx

from app.config.settings import get_settings
from app.services.tts import LANGUAGES

MIN_DURATION_S, MAX_DURATION_S = 15, 300
MAX_SCENES = 20
MAX_HASHTAGS = 8


class ScriptError(RuntimeError):
    pass


@dataclass(frozen=True)
class Platform:
    label: str
    audience: str
    call_to_action: str


PLATFORMS: dict[str, Platform] = {
    "instagram": Platform(
        "Instagram Reel",
        "Instagram users scrolling fast: young and trend-aware. Punchy, energetic and "
        "relatable; short lines with rhythm.",
        "ask viewers to save this reel and follow for more",
    ),
    "facebook": Platform(
        "Facebook Reel",
        "Facebook users of all ages. Warm and story-like, with practical everyday "
        "examples anyone can follow.",
        "ask viewers to share it with someone who needs it and follow for more",
    ),
}


def scene_count(duration_s: int) -> int:
    """About one picture every 7 seconds, at most 8; past ~3 minutes one
    every 20 seconds (15 for 5 minutes), so no picture stays on too long."""
    return max(3, min(8, round(duration_s / 7)), round(duration_s / 20))


def words_per_scene(duration_s: int, language: str, scenes: int) -> int:
    # 0.9: the short pauses between scenes.
    total = duration_s * LANGUAGES[language].words_per_s * 0.9
    return max(6, round(total / scenes))


def build_messages(topic: str, platform: str, language: str, duration_s: int) -> list[dict]:
    p = PLATFORMS[platform]
    lang = LANGUAGES[language].prompt_name
    n = scene_count(duration_s)
    w = words_per_scene(duration_s, language, n)
    sentences = "one or two" if w <= 30 else "a few"
    system = f"""You write voice-over scripts for viral short vertical videos ({p.label}). Reply with JSON only.
Rules:
- Write the title, hooks, every narration and the caption in {lang}. Never add translations or text in brackets.
- Exactly {n} scenes. A narration is what the narrator says during that scene: about {w} words ({w - 4} to {w + 4}), {sentences} short spoken sentences. Never put the whole script into one scene.
- Scene 1 is the hook: a bold claim, a surprising fact or a question that stops the scroll in the first 3 seconds. Scenes 2 to {n - 1} give one point each and keep the viewer curious (hint that the best part is still coming). Scene {n} gives the payoff and ends with a call to action in {lang}: {p.call_to_action}.
- Simple everyday words, as if talking to a friend. No emojis, hashtags or stage directions in the narration.
- image_prompt: in English, one single picture of one concrete scene showing what the narration says: the subject, a detailed colourful setting, dramatic lighting (golden hour, neon glow, moody shadows or rim light) and a striking camera angle, vertical composition. Describe only what is in the picture, never how it is made (no "photo", "illustration" or "painting": the art style is added later). Never a plain white, grey or empty background. Never a collage, montage, split screen or several shots. No text, letters, logos or watermarks.
- hooks: 3 alternative first lines for scene 1, in {lang}, each in a different style (question, bold claim, surprising fact).
- caption: the post caption in {lang}, under 150 characters.
- hashtags: 5 relevant hashtags in English, lowercase, without the # sign.
- music: {MUSIC_RULE}
Audience: {p.audience}"""  # noqa: E501
    user = f"Topic: {topic}\nLength: {duration_s} seconds."
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# Background music the user then finds online (e.g. on YouTube) and adds.
MUSIC_FIELDS = ("mood", "genre", "tempo", "instruments", "search")
MUSIC_RULE = (
    "the background music that fits this reel, all in English: mood (2-3 words), "
    "genre (1-3 words), tempo (pace with a BPM range), instruments (2-4), and search "
    '(a 4-8 word YouTube search phrase ending with "no copyright background music"). '
    "Instrumental only, never louder or busier than the voice-over."
)


def music_schema() -> dict:
    return {
        "type": "object",
        "properties": {f: {"type": "string"} for f in MUSIC_FIELDS},
        "required": list(MUSIC_FIELDS),
    }


def response_schema(scenes: int) -> dict:
    text = {"type": "string"}
    return {
        "type": "object",
        "properties": {
            "title": text,
            "hooks": {"type": "array", "items": text, "minItems": 3, "maxItems": 3},
            "scenes": {
                "type": "array",
                "minItems": scenes,
                "maxItems": scenes,
                "items": {
                    "type": "object",
                    "properties": {"narration": text, "image_prompt": text},
                    "required": ["narration", "image_prompt"],
                },
            },
            "caption": text,
            "hashtags": {"type": "array", "items": text, "minItems": 3, "maxItems": 6},
            "music": music_schema(),
        },
        "required": ["title", "hooks", "scenes", "caption", "hashtags", "music"],
    }


def generate_script(topic: str, platform: str, language: str, duration_s: int) -> dict:
    """Ask Ollama for a script. Raises ScriptError with a message for the UI."""
    n = scene_count(duration_s)
    # Longer reels need a longer reply (Devanagari takes many tokens); past
    # Ollama's default 4096-token window the reply would be cut off.
    num_predict = max(1500, 15 * duration_s)
    raw = _chat(
        build_messages(topic, platform, language, duration_s),
        response_schema(n),
        temperature=0.8,
        num_predict=num_predict,
        num_ctx=max(4096, num_predict + 1024),
    )
    return normalize_script(raw, topic=topic, language=language, scenes=n)


def suggest_music(script: dict, platform: str) -> dict:
    """A music suggestion for an existing script (~5-15 s). Raises ScriptError."""
    narration = " ".join(sc["narration"] for sc in script.get("scenes", []))
    system = "You pick background music for short vertical videos. Reply with JSON only."
    user = (
        f"Platform: {PLATFORMS[platform].label}\n"
        f"Title: {script.get('title', '')}\n"
        f"Voice-over: {narration}\n\n"
        f"Suggest {MUSIC_RULE}"
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    music = clean_music(_chat(messages, music_schema(), temperature=0.7, num_predict=300))
    if music is None:
        raise ScriptError("The model didn't suggest any music. Try again.")
    return music


def _chat(
    messages: list[dict],
    schema: dict,
    *,
    temperature: float,
    num_predict: int,
    num_ctx: int | None = None,
):
    """One Ollama chat call whose reply must follow ``schema``; returns the
    parsed JSON. Raises ScriptError with a message for the UI."""
    settings = get_settings()
    options = {"temperature": temperature, "num_predict": num_predict}
    if num_ctx:
        options["num_ctx"] = num_ctx
    body = {
        "model": settings.reel_script_model,
        "messages": messages,
        "format": schema,
        "stream": False,
        "keep_alive": 0,  # free the RAM for the image model right away
        "options": options,
    }
    try:
        r = httpx.post(
            f"{settings.ollama_url}/api/chat", json=body, timeout=httpx.Timeout(10, read=300)
        )
    except httpx.ConnectError:
        raise ScriptError(
            "Ollama isn't running on this Mac. Open the Ollama app (or run `ollama serve`) "
            "and try again."
        ) from None
    except httpx.HTTPError as exc:
        raise ScriptError(f"Ollama did not answer: {exc}") from None
    if r.status_code == 404:
        raise ScriptError(
            f"The script model isn't installed. Run: ollama pull {settings.reel_script_model}"
        )
    if r.status_code != 200:
        raise ScriptError(f"Ollama error {r.status_code}: {r.text[:300]}")
    try:
        return json.loads(r.json()["message"]["content"])
    except (KeyError, TypeError, ValueError):
        raise ScriptError("The script model sent back something unreadable. Try again.") from None


# ---------------------------------------------------------------------------
# Cleaning up the model's reply
# ---------------------------------------------------------------------------
_SPACES = re.compile(r"\s+")
# "(Wake up early)" after Hindi text, "(Question hook)" after a hook.
_LATIN_BRACKETS = re.compile(r"\s*\([A-Za-z][^()]*\)")
_SENTENCE_END = re.compile(r"(?<=[.!?।])\s+")


def _clean(value: object, *, drop_latin_brackets: bool = False) -> str:
    text = value if isinstance(value, str) else ""
    if drop_latin_brackets:
        text = _LATIN_BRACKETS.sub("", text)
    return _SPACES.sub(" ", text).strip().strip('"“”').strip()


def _tag_char(ch: str) -> bool:
    # Hindi vowel signs (matras) are combining marks, not \w: keep them.
    return ch.isalnum() or ch == "_" or unicodedata.category(ch).startswith("M")


def clean_hashtags(tags: object) -> list[str]:
    out: list[str] = []
    for tag in tags if isinstance(tags, list) else []:
        t = "".join(ch for ch in str(tag).lower() if _tag_char(ch))
        if t and t not in out:
            out.append(t)
    return out[:MAX_HASHTAGS]


def clean_music(raw: object) -> dict | None:
    """The music suggestion with every field a short string; None if empty."""
    if not isinstance(raw, dict):
        return None
    music = {f: _clean(raw.get(f))[:120] for f in MUSIC_FIELDS}
    if not (music["mood"] or music["genre"]):
        return None
    if not music["search"]:
        music["search"] = f"{music['mood']} {music['genre']} no copyright background music".strip()
    return music


def _rebalance(scenes: list[dict], n: int) -> list[dict]:
    """Spread the narration over ``n`` scenes by sentence when the model
    crammed it into a few; the picture prompts are reused in order."""
    sentences = [s for sc in scenes for s in _SENTENCE_END.split(sc["narration"]) if s.strip()]
    n = min(n, len(sentences))
    total = sum(len(s.split()) for s in sentences)
    out: list[dict] = []
    current: list[str] = []
    done = 0
    for i, sentence in enumerate(sentences):
        current.append(sentence)
        done += len(sentence.split())
        left_sentences = len(sentences) - i - 1
        left_scenes = n - len(out) - 1
        if left_scenes and (done >= total * (len(out) + 1) / n or left_sentences == left_scenes):
            out.append({"narration": " ".join(current)})
            current = []
    if current:
        out.append({"narration": " ".join(current)})
    for i, sc in enumerate(out):
        sc["image_prompt"] = scenes[min(i, len(scenes) - 1)]["image_prompt"]
    return out


def normalize_script(raw: object, *, topic: str, language: str, scenes: int) -> dict:
    if not isinstance(raw, dict):
        raise ScriptError("The script model sent back something unreadable. Try again.")
    foreign = language != "en"
    items = []
    for sc in raw.get("scenes") or []:
        if not isinstance(sc, dict):
            continue
        narration = _clean(sc.get("narration"), drop_latin_brackets=foreign)
        if narration:
            prompt = _clean(sc.get("image_prompt")) or topic
            items.append({"narration": narration, "image_prompt": prompt})
    if not items:
        raise ScriptError("The script came back empty. Try again.")

    words = [len(s["narration"].split()) for s in items]
    if len(items) < scenes or max(words) > 2.5 * sum(words) / scenes:
        items = _rebalance(items, scenes)

    hooks = [_clean(h, drop_latin_brackets=True) for h in raw.get("hooks") or []]
    script = {
        "title": _clean(raw.get("title"), drop_latin_brackets=foreign) or topic[:120],
        "hooks": [h for h in hooks if h][:3],
        "scenes": items[:MAX_SCENES],
        "caption": _clean(raw.get("caption"), drop_latin_brackets=foreign),
        "hashtags": clean_hashtags(raw.get("hashtags")),
    }
    music = clean_music(raw.get("music"))
    if music:
        script["music"] = music
    return script
