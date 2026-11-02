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
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from app.config.settings import get_settings
from app.services.reel_render import END_GAP_S, GAP_S, STORY_GAP_S, STORY_PACE
from app.services.romanize import contains_devanagari, romanize
from app.services.tts import LANGUAGES, words_per_s

MIN_DURATION_S, MAX_DURATION_S = 15, 300
MAX_SCENES = 20
MAX_HASHTAGS = 8
MAX_DESCRIPTION = 1500  # the creator's own idea for the script (plot, flow, tone)


@dataclass(frozen=True)
class ScriptModel:
    label: str  # shown in the UI
    slowness: float  # how much longer it takes to write a script than gemma3:4b


# The script writers to pick from (Ollama models; the default is
# REEL_SCRIPT_MODEL). 12B writes clearly better Hindi and knows the old
# stories better, but needs ~8 GB of RAM while it writes.
SCRIPT_MODELS: dict[str, ScriptModel] = {
    "gemma3:4b": ScriptModel("Fast (gemma3 4B)", 1.0),
    "gemma3:12b": ScriptModel("Better Hindi (gemma3 12B, slower)", 2.5),
}
MAX_CHARACTERS = 6  # people/animals with a fixed look (the model is asked for at most 4)
# Characters shown clearly in one picture: more come out with extra arms and
# faces more often, but a story often has three together (Ram, Sita, Lakshman).
MAX_IN_PICTURE = 3


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
        'say), never a summary like "slowly things got better". Read one after another, the '
        "narrations are one flowing story told aloud, not separate captions: each picks up "
        "where the last one ended, with a linking word in the script's language (then, "
        "meanwhile, but, that night), every character is introduced once, and the same "
        'narrator and tense run all through. Scene 1 opens like a told story ("Long ago...") '
        "and still grabs the viewer at once.",
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


def pace(language: str, voice: str | None = None, story: bool = False) -> float:
    """Words a second as the reel reads them (measured per voice): a slower
    voice ("calm") or a story (read a little slower) fit fewer words."""
    return words_per_s(language, voice) * (STORY_PACE if story else 1.0)


# The call to action is written with the script but read after the last scene,
# so the scenes leave room for it (it is ~10 words in every language).
CALL_TO_ACTION_WORDS = 10


# A good script beats an exact length (the creator's wish): a reel may run up
# to 15% long (at most 45 s: a 5 min reel up to 5:45), but hardly short.
LENGTH_OVER = 0.15
LENGTH_OVER_MAX_S = 45.0
LENGTH_UNDER = 0.03
LENGTH_SLACK_S = 3.0  # short reels: this much either way


def length_window(duration_s: int) -> tuple[float, float]:
    """The shortest and longest a reel of ``duration_s`` may come out (s)."""
    under = max(LENGTH_SLACK_S, LENGTH_UNDER * duration_s)
    over = max(LENGTH_SLACK_S, min(LENGTH_OVER_MAX_S, LENGTH_OVER * duration_s))
    return duration_s - under, duration_s + over


def aim_s(duration_s: int) -> float:
    """The length scripts are written for: halfway into the room above
    ``duration_s`` (322.5 s for 5 min), as the model writes short more often."""
    return (duration_s + length_window(duration_s)[1]) / 2


def script_words(
    duration_s: float, language: str, scenes: int, voice: str | None = None, story: bool = False
) -> float:
    """The words that fill ``duration_s`` once the pauses reel_render puts
    between the scenes and after the last one are taken off."""
    gap = STORY_GAP_S if story else GAP_S
    talk = duration_s - gap * (scenes - 1) - END_GAP_S
    return max(scenes, talk) * pace(language, voice, story)


def words_per_scene(
    duration_s: int, language: str, scenes: int, voice: str | None = None, story: bool = False
) -> int:
    words = script_words(aim_s(duration_s), language, scenes, voice, story)
    return max(6, round((words - CALL_TO_ACTION_WORDS) / scenes))


# Extra writing rules for some languages (the small model mixes scripts).
LANGUAGE_NOTES = {
    "hi": " Every word is written in Devanagari, never in Roman letters: no Hinglish like "
    '"karti hai", names and places too (लंका, राम), and English words the way they sound '
    "(रील, वीडियो, आईएएस), even when the creator's idea is written in Hinglish.",
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
    story = script_type == "story"
    w = words_per_scene(duration_s, language, n, voice, story)
    sentences = "one or two" if w <= 30 else "a few"
    structure = SCRIPT_TYPES[script_type].structure.format(n=n, last_middle=n - 1)
    hooks_rule = (
        f"3 alternative opening lines for scene 1, in {lang}, each opening the story in a "
        "different way (a moment, a mystery, a question)."
        if story
        else f"3 alternative first lines for scene 1, in {lang}, each in a different style "
        "(question, bold claim, surprising fact)."
    )
    system = f"""You write voice-over scripts for viral short vertical videos ({p.label}). Reply with JSON only.
Rules:
- Write the title, hooks, every narration and the caption in {lang} only: never words from any other language, never translations or text in brackets.{LANGUAGE_NOTES.get(language, "")}
- Exactly {n} scenes. A narration is what the narrator says during that scene: about {w} words ({w - 4} to {w + 4}), {sentences} short spoken sentences. Never put the whole script into one scene. A narration holds only the words the narrator says: never word counts, labels or notes.
- {structure}
- Every scene adds something new: never repeat a line or an idea, no filler remarks (like "this was very strange"). Keep one point of view all through: a narrator, or the main character speaking as "I".
- Simple everyday words, as if talking to a friend. No emojis, hashtags or stage directions in the narration.
- setting: in English, the time and place of the whole story and what everything around the people looks like: the era, the place, the landscape, the buildings and rooms, the clothes and the everyday objects of that time and place. Never the characters themselves (no names: their looks come from characters). Every picture shows only things that belong there. A story from mythology or history (e.g. the Ramayana) has forests, mountains, rivers, hermitages, ancient palaces, oil lamps, bows and chariots, never modern things such as libraries, book shelves, phones, cars, electric lights or plastic.
- characters: every person or animal seen in the pictures, at most 4, each with a name in English letters (e.g. Ram, Sita, Priya; make one up if the story has none) spelled the same everywhere, and their look in English: age, build, face, skin, hair or fur, clothes and their colours, and anything they carry, fitting the setting, its time and the story's mood. Clothes, jewellery, armour and weapons come from that culture, region and era (ancient India: dhoti, angavastram, sari, gold ornaments, bows, maces; never western, Roman or medieval European armour). Each picture is drawn on its own, so the look is written once, in full, here, and is added to every picture they are in. A narration never says anyone's age, hair, build, face or clothes, not even when someone introduces themselves.
- image_prompt: always in English, even though the narration is in {lang}. One single picture of one concrete moment showing what the narration says: who does what (call the characters by their name only: their look is added from characters), their pose and expression, where in the setting, the lighting (golden hour, moonlight, firelight, moody shadows or rim light) and a striking camera angle, vertical composition. Describe only what is in the picture, never how it is made (no "photo", "illustration" or "painting": the art style is added later). Never a plain white, grey or empty background. Never a collage, montage, split screen or several shots. No text, letters, logos or watermarks.
- In each scene, characters lists the names of the characters seen in its picture, exactly the ones its image_prompt names: at most {MAX_IN_PICTURE} (2 or fewer come out best), shown clearly in a medium or close shot, in simple natural poses (hands at rest or doing one simple thing). crowd is true only when other, unnamed people are seen too (a market, a battle); otherwise nobody else is in the picture, and its image_prompt never adds anyone else: no other person, god, statue, idol or painting of a person (for a prayer, show a lamp, flowers, a symbol or a temple instead).
- call_to_action: one short spoken line in {lang} to {p.call_to_action}. It is read right after the last scene, so never put it in a narration.
- hooks: {hooks_rule}
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
            # Before the scenes: the looks are settled first, then used.
            "setting": text,
            "characters": {
                "type": "array",
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {"name": text, "look": text},
                    "required": ["name", "look"],
                },
            },
            "scenes": {
                "type": "array",
                "minItems": scenes,
                "maxItems": scenes,
                "items": {
                    "type": "object",
                    "properties": {
                        "narration": text,
                        "image_prompt": text,
                        "characters": {"type": "array", "items": text, "maxItems": MAX_IN_PICTURE},
                        "crowd": {"type": "boolean"},
                    },
                    "required": ["narration", "image_prompt", "characters", "crowd"],
                },
            },
            # Its own field: inside the last narration the model often left it out.
            "call_to_action": text,
            "caption": text,
            "hashtags": {"type": "array", "items": text, "minItems": 3, "maxItems": 6},
            "music": music_schema(),
        },
        "required": [
            "title", "hooks", "setting", "characters", "scenes", "call_to_action", "caption",
            "hashtags", "music",
        ],  # fmt: skip
    }


def generate_script(
    topic: str,
    platform: str,
    language: str,
    duration_s: int,
    script_type: str = DEFAULT_SCRIPT_TYPE,
    description: str = "",
    voice: str | None = None,
    *,
    model: str | None = None,
    on_step: Callable[[str], None] | None = None,
) -> dict:
    """Ask Ollama (``model``, else REEL_SCRIPT_MODEL) for a script; ``on_step``
    hears what it does next. Raises ScriptError with a message for the UI."""
    step = on_step or (lambda _: None)
    n = scene_count(duration_s)
    if language == "hi":
        # Written in Hinglish, the idea came back with Roman words ("Lanka").
        topic, description = devanagari_idea(topic, description, model=model)
    # Longer reels need a longer reply (Devanagari takes many tokens); past
    # Ollama's default 4096-token window the reply (or the prompt) would be cut off.
    num_predict = max(2000, 15 * duration_s + 500)  # +500: the setting and the characters' looks
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
                model=model,
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
    step("Checking its length")
    story = script_type == "story"
    fit_length(script["scenes"], language, duration_s, voice, cta, story=story, model=model)
    add_call_to_action(script["scenes"], cta)
    if language == "hi":
        step("Putting every word in Hindi")
        hindi_only(script, model=model)
    step("Checking the pictures' words")
    english_picture_texts(script, model=model)
    return script


_LATIN = re.compile(r"[A-Za-z]")


def devanagari_idea(topic: str, description: str, *, model: str | None = None) -> tuple[str, str]:
    """The creator's topic and idea in Devanagari, when written in Roman
    letters (Hinglish) or English: the model copies the script it is given.
    As they were if this fails."""
    if not (_LATIN.search(topic) or _LATIN.search(description)):
        return topic, description
    messages = [
        {
            "role": "system",
            "content": "You rewrite text in Hindi written in Devanagari. Reply with JSON only.",
        },
        {
            "role": "user",
            "content": "Rewrite both in Hindi, Devanagari script only: write Hindi words given "
            "in Roman letters (Hinglish) in Devanagari, translate English words, keep every "
            "name and the meaning exactly.\n"
            + json.dumps({"topic": topic, "description": description}, ensure_ascii=False),
        },
    ]
    text = {"type": "string"}
    schema = {
        "type": "object",
        "properties": {"topic": text, "description": text},
        "required": ["topic", "description"],
    }
    try:
        raw = _chat(messages, schema, temperature=0.2, num_predict=1200, model=model)
    except ScriptError:
        return topic, description
    new_topic = _clean(raw.get("topic")) if isinstance(raw, dict) else ""
    new_description = _clean(raw.get("description")) if isinstance(raw, dict) else ""
    if not new_topic or (description and not new_description):
        return topic, description
    return new_topic[:500], new_description[:MAX_DESCRIPTION]


def hindi_only(script: dict, *, model: str | None = None) -> None:
    """Every spoken or shown Hindi line in Devanagari: lines the model still
    wrote with Roman letters ("Lanka का राजा", "Reels") are written again.
    Picture prompts and hashtags stay English. A line still not all Devanagari
    after that is kept as it was."""
    slots = [(script, "title"), (script, "caption")]
    slots += [(script["hooks"], i) for i in range(len(script.get("hooks") or []))]
    slots += [(sc, "narration") for sc in script["scenes"]]
    todo = [(holder, key) for holder, key in slots if _LATIN.search(holder[key] or "")]
    if not todo:
        return
    listed = "\n".join(f"{i + 1}. {holder[key]}" for i, (holder, key) in enumerate(todo))
    messages = [
        {
            "role": "system",
            "content": "You correct Hindi text to use Devanagari script only. "
            "Reply with JSON only.",
        },
        {
            "role": "user",
            "content": "Rewrite each line in Hindi using only Devanagari script: names and places "
            "too (Lanka → लंका, Ram → राम), English words the way they sound (Reels → रील्स, "
            "video → वीडियो). Change nothing else.\n" + listed,
        },
    ]
    count = len(todo)
    schema = {
        "type": "object",
        "properties": {
            "lines": {"type": "array", "items": {"type": "string"}, "minItems": count,
                      "maxItems": count}
        },
        "required": ["lines"],
    }  # fmt: skip
    try:
        raw = _chat(
            messages,
            schema,
            temperature=0.2,
            num_predict=200 + 200 * count,
            num_ctx=max(4096, 800 + 400 * count),
            model=model,
        )
    except ScriptError:
        return
    lines = raw.get("lines") if isinstance(raw, dict) else None
    if not isinstance(lines, list) or len(lines) != count:
        return
    for (holder, key), line in zip(todo, lines, strict=True):
        line = _spoken(_clean(line))
        if line and not _LATIN.search(line):
            holder[key] = line


def spoken_seconds(
    narrations: list[str], language: str, voice: str | None = None, story: bool = False
) -> float:
    """About how long the narrations take to read out, with the pauses
    reel_render puts between scenes (0.25 s, a story 0.8 s; 0.8 s after the last)."""
    words = sum(len(t.split()) for t in narrations)
    gap = STORY_GAP_S if story else GAP_S
    return words / pace(language, voice, story) + gap * (len(narrations) - 1) + END_GAP_S


LENGTH_TRIES = 2  # rewrites at most of a script outside length_window


def fit_length(
    scenes: list[dict],
    language: str,
    duration_s: int,
    voice: str | None = None,
    cta: str = "",
    story: bool = False,
    model: str | None = None,
) -> None:
    """Small models don't keep to a word count (a 40 s script came back at
    ~70 s; a 5 min one had 523 words for ~700, its later scenes ever shorter).
    While the narrations (with the call to action still to come) are outside
    length_window - a script a little long is left as it is - the scenes
    furthest off (the long ones when it must get shorter, the short ones when
    it must get longer) are rewritten to aim_s; a rewrite is kept only if it
    comes closer to the window."""
    wps = pace(language, voice, story)
    extra = 0 if not cta or _has_call_to_action(scenes[-1]["narration"]) else len(cta.split())
    n = len(scenes)
    low, high = length_window(duration_s)
    aim = aim_s(duration_s)
    target = script_words(aim, language, n, voice, story) - extra  # words in the scenes

    def seconds(texts: list[str]) -> float:
        return spoken_seconds(texts, language, voice, story) + extra / wps

    def off(texts: list[str]) -> float:
        """Seconds outside the window (0 inside it)."""
        s = seconds(texts)
        return max(low - s, s - high, 0.0)

    for _ in range(LENGTH_TRIES):
        current = [sc["narration"] for sc in scenes]
        if not off(current):
            return
        before = seconds(current)
        shorter = before > high
        counts = [len(t.split()) for t in current]
        each = target / n
        picked = [i for i, c in enumerate(counts) if (c > each if shorter else c < each)]
        kept = sum(c for i, c in enumerate(counts) if i not in picked)
        w = max(4, round((target - kept) / len(picked)))
        try:
            texts = _rewrite_to_length(
                current, picked, w, before, round(aim), language, shorter, model
            )
        except BadReplyError:
            continue
        except ScriptError:
            return
        if texts is None:
            continue
        rewritten = list(current)
        for i, t in zip(picked, texts, strict=True):
            rewritten[i] = t
        if off(rewritten) < off(current):
            for sc, t in zip(scenes, rewritten, strict=True):
                sc["narration"] = t


def _rewrite_to_length(
    narrations: list[str],
    picked: list[int],
    words: int,
    before: float,
    duration_s: int,
    language: str,
    shorter: bool,
    model: str | None,
) -> list[str] | None:
    """The narrations ``picked`` (by index) rewritten to about ``words`` words
    each, the others given as the story around them; None if the reply can't
    be used."""
    n = len(narrations)
    lang = LANGUAGES[language].prompt_name
    keys = [f"scene_{i + 1}" for i in picked]  # the reply's fields, so none gets mixed up
    which = (
        "all of them"
        if len(picked) == n
        else "only scenes " + ", ".join(str(i + 1) for i in picked)
    )
    how = (
        "Cut extra details and repeated words; keep every key moment."
        if shorter
        else "Add concrete details, a short line of dialogue or a feeling; never filler."
    )
    listed = "\n".join(f"Scene {i + 1}: {t}" for i, t in enumerate(narrations))
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
                f"seconds. Rewrite {which} {'shorter' if shorter else 'longer'}: about "
                f"{words} words each ({max(1, words - 3)} to {words + 3}), as "
                f"{', '.join(keys)}. Keep the same story: the same events in the same scenes, "
                f"the same names, all in {lang}.{LANGUAGE_NOTES.get(language, '')} {how} Write "
                f"only the words the narrator says: never word counts or notes.\n{listed}"
            ),
        },
    ]
    text = {"type": "string"}
    schema = {"type": "object", "properties": dict.fromkeys(keys, text), "required": keys}
    # Devanagari takes many tokens.
    num_predict = max(600, round(len(picked) * (words + 4) * 3.5) + 200)
    raw = _chat(
        messages,
        schema,
        temperature=0.4,
        num_predict=num_predict,
        num_ctx=max(4096, num_predict + len(listed) // 2 + 1024),
        model=model,
    )
    texts = [raw.get(k) if isinstance(raw, dict) else None for k in keys]
    if not all(isinstance(t, str) for t in texts):
        return None
    texts = [_spoken(_clean(t, drop_latin_brackets=language != "en")) for t in texts]
    return texts if all(texts) else None


# Letters of any script but Latin (Devanagari, Greek, Cyrillic, CJK, ...).
_NOT_LATIN = re.compile(r"[\u0370-\u1fff\u2c00-\ufdff]")


def english_picture_texts(script: dict, *, model: str | None = None) -> None:
    """The image model needs English, but the script model sometimes writes
    what goes into the pictures (picture prompts, the characters' looks, the
    setting) in the script's language. One more call puts those into English;
    if it fails, they stay as they are."""
    slots = [(sc, "image_prompt") for sc in script["scenes"]]
    slots += [(c, "look") for c in script.get("characters") or []]
    if script.get("setting"):
        slots.append((script, "setting"))
    todo = [(holder, key) for holder, key in slots if _NOT_LATIN.search(holder[key])]
    if not todo:
        return
    listed = "\n".join(f"{i + 1}. {holder[key]}" for i, (holder, key) in enumerate(todo))
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
            model=model,
        )
    except ScriptError:
        return
    prompts = raw.get("prompts") if isinstance(raw, dict) else None
    if not isinstance(prompts, list) or len(prompts) != count:
        return
    for (holder, key), english in zip(todo, prompts, strict=True):
        english = _clean(english)
        if english and not _NOT_LATIN.search(english):
            holder[key] = english


def suggest_music(script: dict, platform: str, *, model: str | None = None) -> dict:
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
    music = clean_music(
        _chat(messages, music_schema(), temperature=0.7, num_predict=300, model=model)
    )
    if music is None:
        raise ScriptError("The model didn't suggest any music. Try again.")
    return music


# ---------------------------------------------------------------------------
# One scene's spoken text rephrased (replacing a word is done on the page,
# exactly, without the model)
# ---------------------------------------------------------------------------
MAX_HINT = 300
# The reel's length comes from the words: a rephrase never has more, and not
# many fewer either. Some room for more letters (a word spelled longer).
MIN_WORDS_SHARE = 0.85
EXTRA_CHARS = 0.10
EDIT_TRIES = 3  # the small model often misses the length once


def _edit_problem(
    text: str, before: str, min_words: int, max_words: int, max_chars: int, language: str
) -> str | None:
    """What is wrong with a rephrased narration, told to the model; None: fine."""
    if not text:
        return "That was empty."
    if _SPACES.sub(" ", text) == _SPACES.sub(" ", before).strip():
        return "That is the line as it was: say it in other words."
    words = len(text.split())
    if words < min_words:
        return f"That has {words} words: too short, it needs {min_words} to {max_words} words."
    if words > max_words or len(text) > max_chars:
        return (
            f"That has {words} words and {len(text)} characters: too long, it needs "
            f"{min_words} to {max_words} words and at most {max_chars} characters."
        )
    if language == "hi" and _LATIN.search(text):
        return "That has words in Roman letters: write every word in Devanagari."
    return None


def rephrase_narration(
    narration: str,
    hint: str,
    language: str,
    *,
    model: str | None = None,
) -> str:
    """One scene's spoken text (``narration``, as on screen, maybe not saved
    yet) said in other words, the way ``hint`` asks if given ("simpler").
    Only this text goes to the model: no other scene, so nothing from them
    comes in. In the reel's language, never more words than now (nor over
    10% more letters) and not under 85% of them, so the reel keeps its
    length. Asked again (twice at most) if it breaks that; then raises
    ScriptError (~5-45 s)."""
    lang = LANGUAGES[language].prompt_name
    max_words = len(narration.split())
    min_words = max(1, int(max_words * MIN_WORDS_SHARE))
    max_chars = round(len(narration) * (1 + EXTRA_CHARS))
    system = f"""You rephrase one line of a voice-over script. Reply with JSON only.
Rules:
- Say the same thing in other words: the same events, people, names, facts and order. Never add anything and never leave anything out.
- Write in {lang} only, whatever language the creator's note is written in.{LANGUAGE_NOTES.get(language, "")}
- {min_words} to {max_words} words and at most {max_chars} characters: never longer than the line is now, and not much shorter.
- Only the words the narrator says: no notes, labels, word counts, quotes or brackets."""  # noqa: E501
    user = "\n".join(
        [
            f"The line: {narration}",
            *([f"How to rephrase it: {hint}"] if hint else []),
            f"Reminder: the new line in {lang}, {min_words} to {max_words} words.",
        ]
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    schema = {
        "type": "object",
        "properties": {"narration": {"type": "string"}},
        "required": ["narration"],
    }
    problem = "It sent back something unreadable."
    for _ in range(EDIT_TRIES):
        try:
            raw = _chat(
                messages,
                schema,
                temperature=0.5,
                num_predict=200 + 3 * max_chars,
                model=model,
            )
        except BadReplyError:
            continue
        text = _spoken(_clean(raw.get("narration") if isinstance(raw, dict) else ""))
        problem = _edit_problem(text, narration, min_words, max_words, max_chars, language)
        if problem is None:
            return text
        reply = json.dumps({"narration": text}, ensure_ascii=False)
        messages += [
            {"role": "assistant", "content": reply},
            {"role": "user", "content": f"{problem} Write it again."},
        ]
    raise ScriptError(f"The AI's version was not used: {problem} Try again.")


def _chat(
    messages: list[dict],
    schema: dict,
    *,
    temperature: float,
    num_predict: int,
    num_ctx: int | None = None,
    model: str | None = None,
):
    """One Ollama chat call (``model``, else REEL_SCRIPT_MODEL) whose reply
    must follow ``schema``; returns the parsed JSON. Raises ScriptError with
    a message for the UI."""
    settings = get_settings()
    options = {"temperature": temperature, "num_predict": num_predict}
    if num_ctx:
        options["num_ctx"] = num_ctx
    body = {
        "model": model or settings.reel_script_model,
        "messages": messages,
        "format": schema,
        "stream": False,
        "keep_alive": 0,  # free the RAM for the image model right away
        "options": options,
    }
    try:
        r = httpx.post(
            # A long script from the 12B model takes minutes.
            f"{settings.ollama_url}/api/chat",
            json=body,
            timeout=httpx.Timeout(10, read=900),
        )
    except httpx.ConnectError:
        raise ScriptError(
            "Ollama isn't running on this Mac. Open the Ollama app (or run `ollama serve`) "
            "and try again."
        ) from None
    except httpx.HTTPError as exc:
        raise ScriptError(f"Ollama did not answer: {exc}") from None
    if r.status_code == 404:
        name = model or settings.reel_script_model
        raise ScriptError(f"The script model isn't installed. Run: ollama pull {name}")
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


# What the model sometimes writes after a narration: the word count it was
# asked for ('…है।”, 20 words. 17 words.', '(18 शब्द)'). Only at the end:
# "learn 5 words a day" in the middle of a line is meant.
_NOTES_TAIL = re.compile(
    r"""(?:\s*["“”'’]?\s*[,;:–—-]?\s*\(?\s*\d+\s*(?:words?|शब्द)\s*\)?\s*[.।]?)+\s*$""",
    re.IGNORECASE,
)


def _spoken(narration: str) -> str:
    """A narration without notes after it."""
    return _NOTES_TAIL.sub("", narration).strip().strip('"“”, ').strip()


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
        source = scenes[min(i, len(scenes) - 1)]
        sc.update({k: v for k, v in source.items() if k != "narration"})  # its picture
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


def _loose(name: str) -> str:
    """A name to compare spellings by: "Raam" (romanized "राम") is "Ram"."""
    name = name.casefold()
    for long, short in (("aa", "a"), ("ee", "i"), ("ii", "i"), ("oo", "u"), ("uu", "u")):
        name = name.replace(long, short)
    return name


def clean_characters(raw: object, image_prompts: list[str] = ()) -> tuple[list[dict], dict]:
    """[{"name", "look"}], each name once, both filled in, the name in English
    letters: a Hindi name ("राम", which the picture prompts call "Ram") would
    read as another person. Also how the model may call each one: {alias: name}."""
    words = {_loose(w): w for w in re.findall(r"[A-Za-z][A-Za-z'-]*", " ".join(image_prompts))}
    out: list[dict] = []
    aliases: dict[str, str] = {}
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict):
            continue
        given, look = _clean(c.get("name"))[:60], _clean(c.get("look"))[:800]
        name = given
        if contains_devanagari(given):
            roman = romanize(given).strip()
            name = words.get(_loose(roman), roman)  # as the picture prompts spell it
        if name and look and name not in {o["name"] for o in out}:
            out.append({"name": name, "look": look})
            for alias in (given.casefold(), name.casefold(), _loose(name)):
                aliases.setdefault(alias, name)
    return out[:MAX_CHARACTERS], aliases


def clean_setting(setting: str, names: list[str]) -> str:
    """The setting without the sentences about the characters: it goes into
    every picture, so "Ram, Sita and Lakshman wear..." drew all three into a
    picture of two."""
    sentences = re.split(r"(?<=[.!?])\s+", setting)
    return " ".join(s for s in sentences if s and not _named_in(s, names))[:1000]


def _named_in(prompt: str, names: list[str]) -> set[str]:
    """The characters an image_prompt mentions (any spelling of long vowels)."""
    text = _loose(prompt)
    return {n for n in names if re.search(rf"(?<![a-z]){re.escape(_loose(n))}(?![a-z])", text)}


def normalize_script(
    raw: object, *, topic: str, language: str, scenes: int, with_call_to_action: bool = True
) -> dict:
    if not isinstance(raw, dict):
        raise BadReplyError("The script model sent back something unreadable. Try again.")
    foreign = language != "en"
    raw_scenes = [sc for sc in raw.get("scenes") or [] if isinstance(sc, dict)]
    prompts = [str(sc.get("image_prompt") or "") for sc in raw_scenes]
    characters, aliases = clean_characters(raw.get("characters"), prompts)
    names = [c["name"] for c in characters]
    items = []
    for sc in raw_scenes:
        narration = _spoken(_clean(sc.get("narration"), drop_latin_brackets=foreign))
        if narration:
            prompt = _clean(sc.get("image_prompt")) or topic
            listed = sc.get("characters") if isinstance(sc.get("characters"), list) else []
            # Who is in the picture: the ones listed and the ones its prompt
            # names (the list once left out a third person the prompt drew).
            wanted = {aliases.get(_clean(n).casefold()) or aliases.get(_loose(_clean(n)))
                      for n in listed} | _named_in(prompt, names)  # fmt: skip
            shown = [n for n in names if n in wanted][:MAX_IN_PICTURE]
            items.append(
                {
                    "narration": narration,
                    "image_prompt": prompt,
                    "characters": shown,
                    "crowd": sc.get("crowd") is True,
                }
            )
    if not items:
        raise BadReplyError("The script came back empty. Try again.")

    words = [len(s["narration"].split()) for s in items]
    if len(items) < scenes or max(words) > 2.5 * sum(words) / scenes:
        items = _rebalance(items, scenes)

    if with_call_to_action:
        add_call_to_action(items[:MAX_SCENES], call_to_action(raw, language))

    hooks = [_clean(h, drop_latin_brackets=True) for h in raw.get("hooks") or []]
    # Same key order as schemas.reel.ReelScript: the page compares the two.
    script = {
        "title": _clean(raw.get("title"), drop_latin_brackets=foreign) or topic[:120],
        "hooks": [h for h in hooks if h][:3],
        "setting": clean_setting(_clean(raw.get("setting")), names),
        "characters": characters,
        "scenes": items[:MAX_SCENES],
        "caption": _clean(raw.get("caption"), drop_latin_brackets=foreign),
        "hashtags": clean_hashtags(raw.get("hashtags")),
    }
    music = clean_music(raw.get("music"))
    if music:
        script["music"] = music
    return script
