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
from app.services.tts import LANGUAGES, voice_speed

MIN_DURATION_S, MAX_DURATION_S = 15, 300
MAX_SCENES = 20
MAX_HASHTAGS = 8
MAX_DESCRIPTION = 1500  # the creator's own idea for the script (plot, flow, tone)


class ScriptError(RuntimeError):
    pass


class BadReplyError(ScriptError):
    """The model answered, but got stuck: an unreadable or empty reply, or
    Ollama stopping it for repeating itself. Another try usually works."""


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


@dataclass(frozen=True)
class ScriptType:
    label: str  # shown in the UI
    # How the scenes hang together; {n} = scenes, {last_middle} = n - 1.
    structure: str


# The old (only) shape was "hook, one tip per scene, payoff": every script came
# out as a list, never a story.
SCRIPT_TYPES: dict[str, ScriptType] = {
    "story": ScriptType(
        "Story",
        "Tell it as one story with a beginning, middle and end, about a main character "
        "with a name. Scene 1 is the hook: drop the viewer into the story in the first 3 "
        "seconds (a bold moment, a mystery or a question). Scenes 2 to {last_middle} each "
        "carry on straight from the scene before: build up the problem, raise the stakes "
        "and turn it with a twist or a climax. Scene {n} gives the story a clear ending "
        "(and its lesson, if it has one). Show concrete moments (what happens, what people "
        'say), never a summary like "slowly things got better".',
    ),
    "tips": ScriptType(
        "Tips / list",
        "Scene 1 is the hook: a bold claim, a surprising fact or a question that stops the "
        "scroll in the first 3 seconds. Scenes 2 to {last_middle} give one point each and "
        "keep the viewer curious (hint that the best part is still coming). Scene {n} gives "
        "the payoff.",
    ),
    "facts": ScriptType(
        "Facts / explainer",
        "Scene 1 is the hook: the most surprising fact or a question that stops the scroll "
        "in the first 3 seconds. Scenes 2 to {last_middle} explain the topic step by step, "
        "one clear fact or example each, every scene building on the one before. Scene {n} "
        "sums up what it means for the viewer.",
    ),
}
DEFAULT_SCRIPT_TYPE = "story"


def scene_count(duration_s: int) -> int:
    """About one picture every 7 seconds, at most 8; past ~3 minutes one
    every 20 seconds (15 for 5 minutes), so no picture stays on too long."""
    return max(3, min(8, round(duration_s / 7)), round(duration_s / 20))


def words_per_scene(duration_s: int, language: str, scenes: int, voice: str | None = None) -> int:
    # 0.9: the short pauses between scenes. A slower voice ("calm") fits fewer words.
    total = duration_s * LANGUAGES[language].words_per_s * voice_speed(voice) * 0.9
    return max(6, round(total / scenes))


# Extra writing rules for some languages (the small model mixes scripts).
LANGUAGE_NOTES = {
    "hi": " Hindi words are always written in Devanagari, never in Roman letters (no "
    'Hinglish like "karti hai"), even when the creator\'s idea is written in Hinglish; '
    "English names and terms people use (like IAS) may stay in English.",
}


def build_messages(
    topic: str,
    platform: str,
    language: str,
    duration_s: int,
    script_type: str = DEFAULT_SCRIPT_TYPE,
    description: str = "",
    voice: str | None = None,
) -> list[dict]:
    p = PLATFORMS[platform]
    lang = LANGUAGES[language].prompt_name
    n = scene_count(duration_s)
    w = words_per_scene(duration_s, language, n, voice)
    sentences = "one or two" if w <= 30 else "a few"
    structure = SCRIPT_TYPES[script_type].structure.format(n=n, last_middle=n - 1)
    system = f"""You write voice-over scripts for viral short vertical videos ({p.label}). Reply with JSON only.
Rules:
- Write the title, hooks, every narration and the caption in {lang} only: never words from any other language, never translations or text in brackets.{LANGUAGE_NOTES.get(language, "")}
- Exactly {n} scenes. A narration is what the narrator says during that scene: about {w} words ({w - 4} to {w + 4}), {sentences} short spoken sentences. Never put the whole script into one scene.
- {structure}
- Every scene adds something new: never repeat a line or an idea, no filler remarks (like "this was very strange"). Keep one point of view all through: a narrator, or the main character speaking as "I".
- Simple everyday words, as if talking to a friend. No emojis, hashtags or stage directions in the narration.
- image_prompt: always in English, even though the narration is in {lang}. One single picture of one concrete scene showing what the narration says: the subject, a detailed colourful setting, dramatic lighting (golden hour, neon glow, moody shadows or rim light) and a striking camera angle, vertical composition. Describe only what is in the picture, never how it is made (no "photo", "illustration" or "painting": the art style is added later). Never a plain white, grey or empty background. Never a collage, montage, split screen or several shots. No text, letters, logos or watermarks.
- A person or animal who is in several scenes is described in full, the same way, in every image_prompt (name, age, build, face, hair or fur, clothes, colours), as each picture is drawn on its own: never "the girl from scene 2". These looks go in image_prompt only: a narration never says anyone's age, hair, build, face or clothes, not even when someone introduces themselves. Spell every name the same way each time.
- call_to_action: one short spoken line in {lang} to {p.call_to_action}. It is read right after the last scene, so never put it in a narration.
- hooks: 3 alternative first lines for scene 1, in {lang}, each in a different style (question, bold claim, surprising fact).
- caption: the post caption in {lang}, under 150 characters.
- hashtags: 5 relevant hashtags in English, lowercase, without the # sign.
- music: {MUSIC_RULE}
Audience: {p.audience}"""  # noqa: E501
    user = f"Topic: {topic}\nLength: {duration_s} seconds."
    if description:
        user += (
            "\nThe creator's own idea for this script (plot, flow, characters, tone, ending). "
            f"Follow it closely, within the rules above:\n{description}"
        )
    elif script_type == "story":
        user += (
            "\nNo plot was given: invent a specific one, with a real problem, a turning point "
            "and a surprising twist."
        )
    # Last, where the small model heeds it most: without it, picture prompts
    # sometimes came back in Hindi.
    user += f"\nReminder: every narration in {lang}, every image_prompt in English."
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
            # Its own field: inside the last narration the model often left it out.
            "call_to_action": text,
            "caption": text,
            "hashtags": {"type": "array", "items": text, "minItems": 3, "maxItems": 6},
            "music": music_schema(),
        },
        "required": ["title", "hooks", "scenes", "call_to_action", "caption", "hashtags", "music"],
    }


def generate_script(
    topic: str,
    platform: str,
    language: str,
    duration_s: int,
    script_type: str = DEFAULT_SCRIPT_TYPE,
    description: str = "",
    voice: str | None = None,
) -> dict:
    """Ask Ollama for a script. Raises ScriptError with a message for the UI."""
    n = scene_count(duration_s)
    # Longer reels need a longer reply (Devanagari takes many tokens); past
    # Ollama's default 4096-token window the reply (or the prompt) would be cut off.
    num_predict = max(1500, 15 * duration_s)
    messages = build_messages(
        topic, platform, language, duration_s, script_type, description, voice
    )
    # About 1 script in 12 the model gets stuck repeating itself; a second
    # try (it writes differently each time) almost always works.
    for attempt in (1, 2):
        try:
            raw = _chat(
                messages,
                response_schema(n),
                temperature=0.8,
                num_predict=num_predict,
                num_ctx=max(4096, num_predict + 2048),  # room for a long description
            )
            script = normalize_script(
                raw, topic=topic, language=language, scenes=n, with_call_to_action=False
            )
            break
        except BadReplyError:
            if attempt == 2:
                raise
    # The call to action goes on after the length is fixed, so a rewrite can't drop it.
    cta = call_to_action(raw, language)
    fit_length(script["scenes"], language, duration_s, voice, cta)
    add_call_to_action(script["scenes"], cta)
    english_picture_prompts(script["scenes"])
    return script


def spoken_seconds(narrations: list[str], language: str, voice: str | None = None) -> float:
    """About how long the narrations take to read out, with the pauses
    reel_render puts between scenes (0.25 s, 0.8 s after the last)."""
    wps = LANGUAGES[language].words_per_s * voice_speed(voice)
    words = sum(len(t.split()) for t in narrations)
    return words / wps + 0.25 * len(narrations) + 0.55


LENGTH_TOLERANCE = 0.2  # a script this far off the reel's length is rewritten once


def fit_length(
    scenes: list[dict], language: str, duration_s: int, voice: str | None = None, cta: str = ""
) -> None:
    """Small models don't keep to a word count (a 40 s script came back at
    ~70 s, a 3 min one at ~2 min 15 s). If the narrations (with the call to
    action still to come) are more than 20% off the reel's length, ask once for
    them rewritten to it; the rewrite is kept only if it is closer."""
    wps = LANGUAGES[language].words_per_s * voice_speed(voice)
    extra = 0 if not cta or _has_call_to_action(scenes[-1]["narration"]) else len(cta.split())

    def seconds(texts: list[str]) -> float:
        return spoken_seconds(texts, language, voice) + extra / wps

    current = [sc["narration"] for sc in scenes]
    before = seconds(current)
    if abs(before - duration_s) <= LENGTH_TOLERANCE * duration_s:
        return
    n = len(scenes)
    w = max(4, round(words_per_scene(duration_s, language, n, voice) - extra / n))
    lang = LANGUAGES[language].prompt_name
    shorter = before > duration_s
    how = (
        "Cut extra details and repeated words; keep every key moment."
        if shorter
        else "Add concrete details, a short line of dialogue or a feeling; never filler."
    )
    listed = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(current))
    messages = [
        {
            "role": "system",
            "content": "You fit voice-over scripts to a length. Reply with JSON only.",
        },
        {
            "role": "user",
            "content": (
                f"These are the {n} narrations of a voice-over in {lang}, one per scene. Read "
                f"aloud they take about {before:.0f} seconds; they must take {duration_s} "
                f"seconds. Rewrite them {'shorter' if shorter else 'longer'}: about {w} words "
                f"each ({max(1, w - 3)} to {w + 3}), exactly {n} narrations in the same order. "
                f"Keep the same story: the same events in the same scenes, the same names, all "
                f"in {lang}.{LANGUAGE_NOTES.get(language, '')} {how}\n{listed}"
            ),
        },
    ]
    text = {"type": "string"}
    schema = {
        "type": "object",
        "properties": {
            "narrations": {"type": "array", "items": text, "minItems": n, "maxItems": n}
        },
        "required": ["narrations"],
    }
    num_predict = max(600, round(n * (w + 4) * 3.5) + 200)  # Devanagari takes many tokens
    try:
        raw = _chat(
            messages,
            schema,
            temperature=0.4,
            num_predict=num_predict,
            num_ctx=max(4096, num_predict + len(listed) // 2 + 1024),
        )
    except ScriptError:
        return
    texts = raw.get("narrations") if isinstance(raw, dict) else None
    if not isinstance(texts, list) or len(texts) != n:
        return
    rewritten = [_clean(t, drop_latin_brackets=language != "en") for t in texts]
    if all(rewritten) and abs(seconds(rewritten) - duration_s) < abs(before - duration_s):
        for sc, t in zip(scenes, rewritten, strict=True):
            sc["narration"] = t


# Letters of any script but Latin (Devanagari, Greek, Cyrillic, CJK, ...).
_NOT_LATIN = re.compile(r"[\u0370-\u1fff\u2c00-\ufdff]")


def english_picture_prompts(scenes: list[dict]) -> None:
    """The image model needs English, but the script model sometimes writes
    picture prompts in the script's language. One more call puts those into
    English; if it fails, they stay as they are."""
    todo = [sc for sc in scenes if _NOT_LATIN.search(sc["image_prompt"])]
    if not todo:
        return
    listed = "\n".join(f"{i + 1}. {sc['image_prompt']}" for i, sc in enumerate(todo))
    messages = [
        {
            "role": "system",
            "content": "You translate picture descriptions into English for an image "
            "generator. Reply with JSON only.",
        },
        {
            "role": "user",
            "content": "Translate each into natural English, keeping every detail (names, "
            f"ages, looks, setting, light, camera angle):\n{listed}",
        },
    ]
    text = {"type": "string"}
    count = len(todo)
    schema = {
        "type": "object",
        "properties": {
            "prompts": {"type": "array", "items": text, "minItems": count, "maxItems": count}
        },
        "required": ["prompts"],
    }
    try:
        raw = _chat(
            messages,
            schema,
            temperature=0.2,
            num_predict=200 + 150 * count,
            num_ctx=max(4096, 600 + 350 * count),
        )
    except ScriptError:
        return
    prompts = raw.get("prompts") if isinstance(raw, dict) else None
    if not isinstance(prompts, list) or len(prompts) != count:
        return
    for sc, prompt in zip(todo, prompts, strict=True):
        prompt = _clean(prompt)
        if prompt and not _NOT_LATIN.search(prompt):
            sc["image_prompt"] = prompt


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
        # 500 "token repeat limit reached": the model got stuck repeating itself.
        error = BadReplyError if r.status_code == 500 else ScriptError
        raise error(f"Ollama error {r.status_code}: {r.text[:300]}")
    try:
        return json.loads(r.json()["message"]["content"])
    except (KeyError, TypeError, ValueError):
        raise BadReplyError("The script model sent back something unreadable. Try again.") from None


# ---------------------------------------------------------------------------
# Cleaning up the model's reply
# ---------------------------------------------------------------------------
_SPACES = re.compile(r"\s+")
# "(Wake up early)" after Hindi text, "(Question hook)" after a hook.
_LATIN_BRACKETS = re.compile(r"\s*\([A-Za-z][^()]*\)")
_SENTENCE_END = re.compile(r"(?<=[.!?।])\s+")
# A call to action the model put into the last narration anyway: "follow"
# together with "reel"/"video"/... ("Follow your dreams" alone is not one).
_CTA_FOLLOW = re.compile(
    r"follow|subscribe|फ़?ॉलो|सब्सक्राइब|sigue|síguenos|suscr[ií]b|abonne|segui|iscriviti|"
    r"siga|inscreva",
    re.IGNORECASE,
)
_CTA_MEDIA = re.compile(
    r"reel|vid[eéê]o|channel|chaîne|canal|page|pagina|página|रील|वीडियो|चैनल|पेज", re.IGNORECASE
)


# Bits of JSON the model sometimes leaks into a text: 'करो!”, “और भूलना।” ,'.
# Only after the end of a sentence: '"hello", "thanks"' is a list of words.
_JSON_JOIN = re.compile(r'(?<=[.!?।])\s*["“”]\s*,\s*["“”]\s*')


def _clean(value: object, *, drop_latin_brackets: bool = False) -> str:
    text = value if isinstance(value, str) else ""
    if drop_latin_brackets:
        text = _LATIN_BRACKETS.sub("", text)
    text = _JSON_JOIN.sub(" ", text)
    return _SPACES.sub(" ", text).strip().strip('"“”, ').strip()


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


def _has_call_to_action(narration: str) -> bool:
    return bool(_CTA_FOLLOW.search(narration) and _CTA_MEDIA.search(narration))


def call_to_action(raw: object, language: str) -> str:
    if not isinstance(raw, dict):
        return ""
    return _clean(raw.get("call_to_action"), drop_latin_brackets=language != "en")


def add_call_to_action(scenes: list[dict], cta: str) -> None:
    """The call to action closes the last scene (spoken and captioned with
    it), unless the model already put one there."""
    last = scenes[-1]
    if cta and cta not in last["narration"] and not _has_call_to_action(last["narration"]):
        last["narration"] = f"{last['narration']} {cta}"


def normalize_script(
    raw: object, *, topic: str, language: str, scenes: int, with_call_to_action: bool = True
) -> dict:
    if not isinstance(raw, dict):
        raise BadReplyError("The script model sent back something unreadable. Try again.")
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
        raise BadReplyError("The script came back empty. Try again.")

    words = [len(s["narration"].split()) for s in items]
    if len(items) < scenes or max(words) > 2.5 * sum(words) / scenes:
        items = _rebalance(items, scenes)

    if with_call_to_action:
        add_call_to_action(items[:MAX_SCENES], call_to_action(raw, language))

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
